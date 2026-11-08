"""Claude 委派 Codex 生图：独立连接、真实文件回执，不复用用户的 Codex 对话。"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path

from .codex import CodexConn
from .engines.base import handoff

INSTRUCTIONS = """你只负责本次图片生成或编辑，不执行其他开发任务，不继续委派其他代理。
回复用简体中文。使用内置 image_gen 工具，先按 imagegen 技能操作；不要改用收费API、寻找密钥、安装软件或用代码伪造图片。
参考图应先查看，严格遵守提示词中的编辑不变量。只在当前任务目录保存最终图片，不修改任何其他文件，不提交或推送。
内置生成结果可能先存入 Codex 的 generated_images；必须把本次选定的真实图片复制到当前任务目录。
完成后在当前目录写 result.json，格式为 {"images":["图片绝对路径", ...]}，只列最终图片。
最终回复最后一行写“交给你：”加最终图片绝对路径。不要只用 generatedImage 展示后就声称已经交付。
工具不可用、生成失败或没有真实图片时，明确报告错误，不能拿原图、占位图或旧图片冒充结果。
"""


def image_file(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    with path.open('rb') as f:
        head = f.read(16)
    return (head.startswith(b'\x89PNG\r\n\x1a\n') or head.startswith(b'\xff\xd8\xff')
            or head.startswith((b'GIF87a', b'GIF89a')) or (head[:4] == b'RIFF' and head[8:12] == b'WEBP'))


class ImageDelegator:
    def __init__(self, records: Path):
        self.records = records
        self.sem = asyncio.Semaphore(1)
        # 服务重启后不伪称旧任务仍在运行，也不自动重复提交生图。
        if records.is_dir():
            for p in records.glob('*.json'):
                try:
                    row = json.loads(p.read_text())
                    if row.get('state') in ('queued', 'running'):
                        row.update(state='interrupted', ended=time.time(), error='服务已重启，委派结果待检查，未自动重试')
                        self._save(row)
                except (OSError, ValueError):
                    pass

    def _save(self, row: dict) -> None:
        self.records.mkdir(parents=True, exist_ok=True)
        target = self.records / f"{row['job_id']}.json"
        tmp = target.with_suffix('.tmp')
        tmp.write_text(json.dumps(row, ensure_ascii=False))
        tmp.replace(target)

    async def run(self, *, prompt: str, references: list[str], cwd: str, output_dir: str,
                  add_dirs: list[str], parent_num: int, progress, approve, timeout: float = 1200) -> dict:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError('请给出图片生成或编辑要求')
        if not isinstance(references, list) or any(not isinstance(p, str) for p in references):
            raise ValueError('参考图应为图片绝对路径列表')
        refs = [Path(p).expanduser().resolve() for p in references]
        if any(not Path(raw).expanduser().is_absolute() or not image_file(p) for raw, p in zip(references, refs)):
            raise ValueError('参考图不存在或不是支持的图片，请提供本机图片绝对路径')
        roots = [Path(p).expanduser().resolve() for p in [cwd, *add_dirs]]
        destination = Path(output_dir).expanduser() if output_dir else roots[0] / 'generated_images'
        if not destination.is_absolute():
            destination = roots[0] / destination
        destination = destination.resolve()
        if not any(destination.is_relative_to(p) for p in roots):
            raise ValueError('输出目录应位于当前工作目录或已授权的附加目录中')
        job_id = secrets.token_hex(8)
        out = destination / f'antou-{job_id}'
        out.mkdir(parents=True, exist_ok=False)
        row = dict(job_id=job_id, parent_num=parent_num, state='queued', created=time.time(), output_dir=str(out), images=[])
        self._save(row)
        progress(job_id, 'queued', '等待 Codex 生图')
        conn = None
        messages = []

        def event(method, params):
            item = params.get('item') or {}
            kind = item.get('type')
            if method == 'item/completed' and kind == 'agentMessage':
                messages.append(str(item.get('text') or ''))
            if method == 'item/started':
                stage = 'Codex 正在生成图片' if kind == 'imageGeneration' else 'Codex 正在处理图片任务'
                progress(job_id, 'running', stage)
            if method == 'error' and not params.get('willRetry'):
                row['error'] = str((params.get('error') or {}).get('message') or params.get('message') or '')[:300]

        try:
            async with asyncio.timeout(timeout):
                async with self.sem:
                    row.update(state='running', started=time.time())
                    self._save(row)
                    progress(job_id, 'running', 'Codex 正在准备生图')
                    conn = CodexConn(str(out), event, approve)
                    await conn.start()
                    row['thread_id'] = await conn.open_thread(None, INSTRUCTIONS)
                    self._save(row)
                    request = '请完成以下图片任务。参考图片如有提供，仅用于本次任务。\n\n' + prompt.strip()
                    await conn.start_turn(request, refs, 'medium')
                    await conn.done.wait()
                    if conn.turn_status != 'completed':
                        raise RuntimeError(conn.turn_error or row.get('error') or 'Codex 生图没有正常完成')
                    candidates = []
                    manifest = out / 'result.json'
                    if manifest.is_file() and not manifest.is_symlink():
                        obj = json.loads(manifest.read_text())
                        candidates = obj.get('images') if isinstance(obj, dict) else None
                        if not isinstance(candidates, list) or any(not isinstance(p, str) for p in candidates):
                            raise ValueError('生图结果清单格式不正确')
                    if not candidates:
                        _, candidates, _ = handoff('\n'.join(messages), str(out))
                    images = []
                    for raw in candidates:
                        p = Path(raw).expanduser()
                        if not p.is_absolute():
                            p = out / p
                        if p.is_symlink():
                            raise ValueError('生图结果不能是符号链接')
                        p = p.resolve()
                        if not p.is_relative_to(out) or not image_file(p):
                            raise ValueError('生图结果未保存在任务目录中，或图片不存在')
                        if str(p) not in images:
                            images.append(str(p))
                    if not images:
                        raise RuntimeError('Codex 没有返回真实图片文件，未登记任何附件')
                    row.update(state='completed', ended=time.time(), images=images)
                    self._save(row)
                    progress(job_id, 'completed', f'Codex 已生成 {len(images)} 张图片')
                    return dict(job_id=job_id, state='completed', images=images, output_dir=str(out))
        except (asyncio.CancelledError, TimeoutError) as exc:
            state = 'cancelled' if isinstance(exc, asyncio.CancelledError) else 'timeout'
            row.update(state=state, ended=time.time(), error='委派已取消' if state == 'cancelled' else 'Codex 生图等待超时')
            self._save(row)
            progress(job_id, state, row['error'])
            if conn:
                try:
                    await asyncio.wait_for(conn.interrupt(), 5)
                except Exception:
                    pass
            if state == 'cancelled':
                raise
            return dict(job_id=job_id, state=state, error=row['error'], images=[])
        except Exception as exc:
            row.update(state='failed', ended=time.time(), error=str(exc)[:500])
            self._save(row)
            progress(job_id, 'failed', 'Codex 生图失败')
            return dict(job_id=job_id, state='failed', error=row['error'], images=[])
        finally:
            if conn:
                await conn.close()
