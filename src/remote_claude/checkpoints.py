"""Codex 每轮的工作文件快照；恢复只处理该轮差异，检查冲突并保留撤销副本。"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import uuid

SKIP = {'.git', '.venv', 'node_modules', '__pycache__', '.pytest_cache'}
MAX_FILE = 300 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024

class Checkpoints:
    def __init__(self, base: Path):
        self.base = base.resolve()
        self.objects = self.base / 'objects'
        self.objects.mkdir(parents=True, exist_ok=True)

    def _save(self, value: dict) -> str:
        key = uuid.uuid4().hex
        (self.base / (key + '.json')).write_text(json.dumps(value))
        return key

    def _load(self, key: str) -> dict:
        if len(key) != 32 or any(c not in '0123456789abcdef' for c in key):
            raise ValueError('没有可用的文件快照')
        try:
            return json.loads((self.base / (key + '.json')).read_text())
        except (OSError, ValueError) as e:
            raise ValueError('文件快照缺失，文件未恢复') from e

    def _state(self, path: Path, save=False):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            return {'kind': 'link', 'target': os.readlink(path)}
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f'无法恢复非普通文件：{path.name}')
        if info.st_size > MAX_FILE:
            raise ValueError(f'文件超过快照上限：{path.name}')
        h = hashlib.sha256()
        with path.open('rb') as f:
            while block := f.read(1024 * 1024):
                h.update(block)
        digest = h.hexdigest()
        if save and not (self.objects / digest).exists():
            tmp = self.objects / (uuid.uuid4().hex + '.tmp')
            try:
                with path.open('rb') as src, tmp.open('wb') as out:
                    while block := src.read(1024 * 1024):
                        out.write(block)
                with tmp.open('rb') as check:
                    if hashlib.file_digest(check, 'sha256').hexdigest() != digest:
                        raise ValueError(f'文件正在变化：{path.name}')
                os.replace(tmp, self.objects / digest)
            finally:
                tmp.unlink(missing_ok=True)
        return {'kind': 'file', 'hash': digest, 'mode': stat.S_IMODE(info.st_mode)}

    def capture(self, roots: list[str]) -> str:
        files, errors, total = {}, [], 0
        for root in sorted({str(Path(p).expanduser().resolve()) for p in roots}):
            folder = Path(root)
            try:
                git = subprocess.run(['git', '-C', root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard', '--', '.'], capture_output=True)
                if git.returncode == 0:
                    paths = [folder / os.fsdecode(n) for n in git.stdout.split(b'\0') if n]
                else:
                    paths = []
                    for parent, dirs, names in os.walk(folder, followlinks=False):
                        for d in list(dirs):
                            if d in SKIP or (Path(parent) / d).resolve().is_relative_to(self.base):
                                dirs.remove(d)
                            elif (Path(parent) / d).is_symlink():
                                paths.append(Path(parent) / d); dirs.remove(d)
                        paths.extend(Path(parent) / n for n in names)
                for path in sorted(set(paths)):
                    if path.is_relative_to(self.base):
                        continue
                    # 不跟随符号链接目录，不触碰工作目录之外的文件。
                    if not path.parent.resolve().is_relative_to(folder):
                        raise ValueError('快照路径越界')
                    if path.is_dir() and not path.is_symlink():
                        continue  # Git 子模块内容由其自身项目管理
                    if path.exists() and not path.is_symlink():
                        total += path.stat().st_size
                        if total > MAX_TOTAL:
                            raise ValueError('工作文件超过 1GB 快照总量上限')
                    state = self._state(path, save=True)
                    if state is not None:
                        files[str(path)] = state
            except (OSError, ValueError) as e:
                errors.append(str(e))
        return self._save({'roots': roots, 'files': files, 'complete': not errors, 'errors': errors})

    def plan(self, pairs: list[tuple[str, str]]) -> dict:
        snapshots = []
        for before, after in pairs:
            a, b = self._load(before), self._load(after)
            if not a.get('complete') or not b.get('complete'):
                raise ValueError('这几轮的快照不完整，文件保持现状')
            snapshots.append((a, b))
        changed = set()
        for a, b in snapshots:
            changed.update(p for p in a['files'].keys() | b['files'].keys() if a['files'].get(p) != b['files'].get(p))
        changes = {}
        for p in changed:
            first, previous = snapshots[0][0]['files'].get(p), snapshots[0][0]['files'].get(p)
            for a, b in snapshots:
                if a['files'].get(p) != previous:
                    raise ValueError(f'轮次之间还有其他改动，未恢复：{Path(p).name}')
                previous = b['files'].get(p)
            if first != previous:
                changes[p] = {'before': previous, 'after': first}
        roots = sorted({str(Path(p).resolve()) for a, b in snapshots for p in a['roots']})
        return {'roots': roots, 'changes': changes}

    def _check_path(self, p: Path, roots: list[str]):
        if not any(p.is_relative_to(Path(r)) and p.parent.resolve().is_relative_to(Path(r)) for r in roots):
            raise ValueError('恢复路径已变化，文件保持现状')
        # 即使链接指向目录内也不沿着链接写入。
        for parent in p.parents:
            if parent.is_symlink():
                raise ValueError('恢复路径包含符号链接目录，文件保持现状')

    def _write(self, p: Path, state):
        if state is None:
            p.unlink(missing_ok=True)
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name('.antou-restore-' + uuid.uuid4().hex)
        try:
            if state['kind'] == 'link':
                tmp.symlink_to(state['target'])
            else:
                import shutil
                shutil.copyfile(self.objects / state['hash'], tmp)
                tmp.chmod(state['mode'])
            os.replace(tmp, p)
        finally:
            tmp.unlink(missing_ok=True)

    def apply(self, plan: dict) -> str:
        changes = plan['changes']
        for name, change in changes.items():
            p = Path(name)
            self._check_path(p, plan['roots'])
            if self._state(p) != change['before']:
                raise ValueError(f'文件已有后续改动，未恢复：{p.name}')
            for state in (change['before'], change['after']):
                if state and state['kind'] == 'file':
                    obj = self.objects / state['hash']
                    with obj.open('rb') as f:
                        if hashlib.file_digest(f, 'sha256').hexdigest() != state['hash']:
                            raise ValueError('快照校验失败，文件保持现状')
        # 先把撤销计划落盘，再操作文件；每个文件用原子替换。
        undo = self._save({'roots': plan['roots'], 'changes': {p: {'before': x['after'], 'after': x['before']} for p, x in changes.items()}})
        done = []
        try:
            for name, change in changes.items():
                p = Path(name)
                if self._state(p) != change['before']:
                    raise ValueError(f'恢复时文件发生变化：{p.name}')
                self._write(p, change['after']); done.append(name)
        except Exception:
            for name in reversed(done):
                if self._state(Path(name)) == changes[name]['after']:
                    self._write(Path(name), changes[name]['before'])
            raise
        return undo

    def undo(self, key: str):
        return self.apply(self._load(key))
