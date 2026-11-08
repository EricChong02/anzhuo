"""演示模式：起一个只有网页的远窗（远窗），里面是几段虚构的对话。
不连 Claude / Codex、不花一分钱、不碰你的文件夹和钥匙串，退出后留下的只是一个临时文件夹。
用来先看看界面长什么样，或者截图做文档。

用法（在仓库根）：.venv/bin/python scripts/demo.py [端口，默认 8799]
然后浏览器打开 http://127.0.0.1:8799/_x/ ，账号 demo，密码 demo。
在里面发话会得到一句预先写好的回答（演示用，不是真的 AI）。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace as M

from anzhuo.web import hash_password

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
TMP = Path(tempfile.mkdtemp(prefix="anzhuo-demo-"))
os.environ["ANZHUO_WEB_HASH"] = hash_password("demo")

from anzhuo import thread, translate  # noqa: E402  环境变量要先设好
from anzhuo.app import Bridge  # noqa: E402
from anzhuo.config import Config  # noqa: E402
from anzhuo.dingtalk import NoDingTalk  # noqa: E402
from anzhuo.report import Turn  # noqa: E402
from anzhuo.sessions import Runner, Session  # noqa: E402

translate.enabled = False
NOW = time.time()


def U(text, i):
    return M(type="user", uuid=f"u{i}", message={"content": text})


def A(blocks, i):
    return M(type="assistant", uuid=f"a{i}", message={"content": blocks})


def tool(name, **inp):
    return {"type": "tool_use", "name": name, "input": inp}


def say(text):
    return {"type": "text", "text": text}


SCRIPTS = {
    "demo-1": [
        U("登录页点“登录”没反应，帮我看看", 1),
        A([say("我先看看登录按钮绑的是哪个函数。"), tool("Grep", pattern="handleLogin"), tool("Read", file_path="src/pages/Login.tsx")], 2),
        A([say("找到了：提交时 `await` 写在了不是 async 的函数里，报错被吞掉了。"), tool("Edit", file_path="src/pages/Login.tsx"),
           tool("Bash", command="npm test -- Login", description="跑登录页的测试")], 3),
        A([say("**做了什么**：把 `handleLogin` 改成 async，并把失败原因显示在输入框下面。\n\n**自查结果**：登录页的 6 个测试全部通过，"
               "手动试了错误密码和空密码两种情况，提示都正常。\n\n**改了哪些**：`src/pages/Login.tsx`（1 个文件）")], 4),
    ],
    "demo-2": [
        U("周报格式想统一一下，你看怎么弄", 1),
        A([say("我翻了最近 4 期周报，格式各不相同。"), tool("Glob", pattern="weekly/*.md"), tool("Read", file_path="weekly/2026-10-24.md")], 2),
        A([say("要你定：周报用哪种格式统一？\n\n"
               "A. 按项目分组，每个项目 3 行（进展 / 问题 / 下周）\n"
               "B. 按时间线写，一天一行\n"
               "C. 只列结论和数字，细节放附件\n\n"
               "推荐：A，团队里看周报的人最关心每个项目到哪了。")], 3),
    ],
    "demo-3": [
        U("把 README 里的安装步骤再核对一遍，缺什么补上", 1),
        A([say("我对着干净的环境一步步走一遍。"), tool("Read", file_path="README.md"), tool("Bash", command="uv sync", description="装依赖")], 2),
        A([say("核对完了，补了两处：\n\n- 第 2 步缺少 Python 版本要求（需要 3.13 以上）\n- 第 4 步的命令少了 `--frozen`\n\n"
               "改动在 `README.md`，已经提交。")], 3),
    ],
}


class Fake:
    """替换真正干活的那一步：演示里不连 AI，发话就回一句固定的话。"""

    async def run(self, s, job, turn):
        turn.steps += ["看 README.md", "改 README.md"]
        await asyncio.sleep(1.2)
        turn.final_text = "收到。这是演示模式，我不会真的去做事——装好 Claude Code 并运行 `anzhuo run` 后，这里就是真的 AI 在回答你。"
        s.claude_id = s.claude_id or f"demo-{s.num}"


async def no_usage(self, force=False):
    self.limits = {"five_hour": (0.37, "21:00"), "seven_day": (0.22, "")}


def build() -> Bridge:
    (TMP / "my-app").mkdir()
    (TMP / "notes").mkdir()
    cfg = Config(web_base=f"http://127.0.0.1:{PORT}", web_port=PORT, web_user="demo", scratch_root=str(TMP / "scratch"),
                 folders={"my-app": str(TMP / "my-app"), "notes": str(TMP / "notes")}, translate_english=False,
                 auto_route=False,   # 演示不连 AI：分派员也关掉
                 super_agent=os.environ.get("ANZHUO_DEMO_SUPER") == "1")   # ANZHUO_DEMO_SUPER=1：带上“总管”对话
    Runner._run = lambda self, s, job, turn: Fake().run(s, job, turn)
    Runner.refresh_usage = no_usage

    def messages(session_id, cwd, times=None):
        msgs = SCRIPTS.get(session_id, [])
        if times is not None:
            for k, m in enumerate(msgs):
                times[m.uuid] = NOW - 3600 + k * 60
        return msgs

    thread.full_messages = messages
    b = Bridge(cfg, NoDingTalk(), data_dir=TMP / "data")
    b.web.config_path = TMP / "config.toml"   # 设置页读写演示用的临时配置，不碰你真的 config.toml
    b.web.config_path.write_text("# 演示用的配置（临时文件）\nsuper_agent = %s\n" % ("true" if cfg.super_agent else "false"),
                                 encoding="utf-8")
    reg = b.registry
    rows = [(1, "修登录页的报错", "demo-1", str(TMP / "my-app"), NOW - 600, 0.0),
            (2, "周报格式统一", "demo-2", str(TMP / "notes"), NOW - 1500, 0.0),
            (3, "核对 README 安装步骤", "demo-3", str(TMP / "my-app"), NOW - 7200, NOW - 7000),
            (4, "整理发票报销", "demo-4", str(TMP / "notes"), NOW - 90000, NOW - 80000)]
    for n, name, cid, cwd, upd, seen in rows:
        reg.add(Session(num=n, name=name, cwd=cwd, claude_id=cid, model="claude-opus-5-5", updated=upd, seen=seen, worked=True,
                        context_pct=[34, 51, 18, 12][n - 1]))
    reg.get(2).awaiting, reg.get(2).awaiting_kind = "周报用哪种格式统一？", "decide"
    reg.current = 1
    for n, cid, req, final, files, cmds in [
            (1, "demo-1", "登录页点“登录”没反应，帮我看看", SCRIPTS["demo-1"][-1].message["content"][0]["text"], ["src/pages/Login.tsx"], ["npm test -- Login"]),
            (3, "demo-3", "把 README 里的安装步骤再核对一遍", SCRIPTS["demo-3"][-1].message["content"][0]["text"], ["README.md"], ["uv sync"])]:
        t = Turn(started=NOW - 700, ended=NOW - 600, request=req, final_text=final, changed_files=files, commands=cmds,
                 steps=["看 Login.tsx", "改 Login.tsx", "跑命令：npm test"], model="claude-opus-5-5", effort="medium",
                 timeline=[{"t": NOW - 690, "kind": "say", "text": "我先看看登录按钮绑的是哪个函数。"},
                           {"t": NOW - 650, "kind": "do", "text": "改 " + files[0], "detail": files[0]},
                           {"t": NOW - 620, "kind": "do", "text": "跑命令：" + cmds[0], "detail": cmds[0]}])
        b.web.save_turn(f"#{n} demo", t, reg.get(n).cwd, "做完了")
    reg.save()
    # 一个正在做的对话：有步骤清单和进度
    live = Session(num=5, name="生成接口文档", cwd=str(TMP / "my-app"), claude_id="demo-5", model="claude-sonnet-5-5", updated=NOW - 20,
                   seen=NOW, worked=True, context_pct=27)
    reg.add(live)
    reg.next_num = max(reg.sessions) + 1   # 上面是手工编号，新开的对话从后面接着排
    if cfg.super_agent:   # 总管排在演示对话后面（上面手工编号会盖掉一开始建的那个）
        from anzhuo import superagent
        superagent.ensure(b)
    SCRIPTS["demo-5"] = [U("把 /api 下所有接口整理成文档，放到 docs/api.md", 1)]
    turn = Turn(started=NOW - 260, request="把 /api 下所有接口整理成文档", model="claude-sonnet-5-5", effort="medium",
                steps=["搜：@router", "看 users.py", "看 orders.py", "写 api.md"], last_at=NOW - 8,
                plan=[{"text": "找出所有接口", "status": "done"}, {"text": "读参数和返回值", "status": "done"},
                      {"text": "写 docs/api.md", "status": "doing"}, {"text": "核对示例请求", "status": "pending"}],
                timeline=[{"t": NOW - 250, "kind": "say", "text": "先把路由都找出来。"},
                          {"t": NOW - 200, "kind": "do", "text": "搜：@router", "detail": "@router"},
                          {"t": NOW - 60, "kind": "do", "text": "写 api.md", "detail": "docs/api.md"}])
    b.runner.turns[5] = turn
    b.runner.limits = {"five_hour": (0.37, "21:00"), "seven_day": (0.22, "")}
    return b


async def main():
    b = build()
    await b.web.start()
    print(f"\n演示已启动：http://127.0.0.1:{PORT}/_x/   账号 demo   密码 demo\n（按 Ctrl+C 结束；临时文件在 {TMP}）\n", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
