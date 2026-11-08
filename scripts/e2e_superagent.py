"""总管端到端测试：真的 Claude（Haiku，最便宜），独立的临时数据目录，不碰正在运行的远窗，会消耗少量额度。

用法（在仓库根；先去掉当前终端里的 ANTHROPIC_* / CLAUDE_* 环境变量，免得用错账号）：
  .venv/bin/python scripts/e2e_superagent.py               # 全部场景，约 15 分钟
  .venv/bin/python scripts/e2e_superagent.py dispatch direct  # 只跑某几个
场景：dispatch followup direct humandirect handback parallel decision status folder noself stop full
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import time
import traceback
from pathlib import Path

from anzhuo import split, superagent
from anzhuo.app import Bridge
from anzhuo.config import Config
from anzhuo.dingtalk import Incoming

ROOT = Path(tempfile.mkdtemp(prefix="sa-e2e-"))
LOG = ROOT / "e2e.log"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    handlers=[logging.FileHandler(LOG, encoding="utf-8")])


class DT:
    """假钉钉：网页开着时它不该收到东西；收到就记下来。"""
    def __init__(self):
        self.sent = []

    async def send_markdown(self, uid, title, text): self.sent.append((title, text))
    async def send_text(self, uid, text): self.sent.append(("text", text))
    async def send_image(self, uid, path): self.sent.append(("image", str(path)))
    async def send_file(self, uid, path): self.sent.append(("file", str(path)))
    async def create_card(self, uid, oid, data): pass
    async def update_card(self, oid, data): pass
    async def download(self, code, dest): return dest


def web(text):
    return Incoming(sender_id="me", sender_nick="", conversation_type="1", kind="text", text=text, via="web")


class Harness:
    def __init__(self):
        proj = ROOT / "proj"
        proj.mkdir()
        (proj / "a.txt").write_text("hello world\n", encoding="utf-8")
        self.proj = proj
        cfg = Config(web_base="http://127.0.0.1:1", super_agent=True,
                     split=split.normalize({"simple": "haiku", "dev": "haiku", "design": "haiku"}),
                     scratch_root=str(ROOT / "scratch"), folders={"e2e": str(proj)}, max_parallel=3)
        self.dt = DT()
        self.b = Bridge(cfg, self.dt, data_dir=ROOT / "data")
        self.b.runner.auto_title = False
        self.pushes = []

        async def push(title, body, num=None, only=None):
            self.pushes.append((num, title, body))
            return 1
        self.b.web.push.send = push
        self.router = superagent.find(self.b)
        self.events = []   # (时间, 对话, 这一轮从哪来, 用了哪些工具, 回复)
        orig = self.b._turn_done

        async def spy(s, turn):
            tools = [f'{e.get("text", "")} {e.get("detail", "")}' for e in turn.timeline if e.get("kind") == "do"]
            self.events.append({"t": round(time.time() - self.t0, 1), "num": s.num, "via": turn.via, "role": s.role,
                                "req": getattr(turn, "sa_text", "") or turn.request, "tools": tools, "steps": list(turn.steps), "error": turn.error,
                                "is_error": turn.is_error, "reply": turn.final_text})
            await orig(s, turn)
        self.b._turn_done = spy
        self.b.runner.on_done = spy
        self.t0 = time.time()

    def workers(self):
        return [s for s in self.b.registry.sessions.values() if s.role != "router"]

    def busy(self):
        r = self.b.runner
        return [n for n in self.b.registry.sessions if r.busy(n)]

    async def settle(self, timeout=300, quiet=6):
        """等到所有对话都空下来、而且连续 quiet 秒没有新动静（回报会接着触发总管）。"""
        end, calm = time.time() + timeout, 0.0
        while time.time() < end:
            await asyncio.sleep(1)
            calm = calm + 1 if not self.busy() else 0
            if calm >= quiet:
                return True
        return False

    async def say(self, text, timeout=300):
        n0 = len(self.events)
        await self.b._submit(self.router, text, web(text))
        ok = await self.settle(timeout)
        return ok, self.events[n0:]


ALIAS = {"new_session": "派活", "send_to_session": "转给", "list_sessions": "看对话列表", "stop_session": "停下", "session_status": "看 #"}


def used(turns, name):
    return any(name in str(t) or str(t).startswith(ALIAS[name]) for e in turns for t in e["tools"])


def router_turns(evs, h):
    return [e for e in evs if e["num"] == h.router.num]


def show(evs):
    for e in evs:
        who = "总管" if e["role"] == "router" else f"#{e['num']}"
        print(f"  [{e['t']:>6}s] {who} via={e['via']} tools={e['tools'][:6]} err={e['error'][:80] if e['error'] else ''}")
        print("           " + (e["reply"] or "").replace("\n", " ⏎ ")[:300])


RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  —— {detail}" if detail and not cond else ""))


async def s_dispatch(h):
    ok, evs = await h.say("帮我写一个 hello.py，打印 hello，然后运行它告诉我输出")
    show(evs)
    check("dispatch: 全部做完没有超时", ok)
    ws = h.workers()
    check("dispatch: 开了 1 个新对话", len(ws) == 1, f"workers={[w.num for w in ws]}")
    w = ws[0] if ws else None
    check("dispatch: 新对话记着是总管派的", w and w.boss == h.router.num)
    rt = router_turns(evs, h)
    check("dispatch: 总管第一轮用了 new_session", rt and used(rt[:1], "new_session"), str(rt[0]["tools"] if rt else ""))
    rdir = Path(h.router.cwd)
    check("dispatch: 总管没亲自写文件（它的文件夹里没东西）", not any(rdir.iterdir()), str(list(rdir.iterdir())))
    check("dispatch: worker 不在总管的文件夹里干活", w and Path(w.cwd).resolve() != rdir.resolve(), w and w.cwd)
    check("dispatch: worker 用 report 结构化汇报了", w and w.ticket in ("ready", "waiting", "asked", "blocked"), w and w.ticket)
    check("dispatch: worker 那一轮调了 report", any(e["num"] == (w and w.num) and any(str(t).startswith("汇报") for t in e["tools"]) for e in evs))
    check("dispatch: worker 那一轮是 via=router", any(e["num"] == (w and w.num) and e["via"] == "router" for e in evs))
    check("dispatch: 总管收到回报并又跑了一轮", len(rt) >= 2 and rt[-1]["via"] == "report")
    check("dispatch: 最终回复提到 hello", rt and "hello" in (rt[-1]["reply"] or "").lower())
    check("dispatch: worker 那一轮没直接推送给用户", all(p[0] == h.router.num for p in h.pushes), str([p[0] for p in h.pushes]))
    return w


async def s_followup(h, w):
    n = len(h.workers())
    ok, evs = await h.say(f"让刚才那个对话把 hello.py 改成同时打印当前日期，再运行一次")
    show(evs)
    check("followup: 没超时", ok)
    check("followup: 没开新对话", len(h.workers()) == n, f"{n} → {len(h.workers())}")
    rt = router_turns(evs, h)
    check("followup: 用 send_to_session 交给原对话", used(rt, "send_to_session"))
    check("followup: 原对话又干了一轮", any(e["num"] == w.num for e in evs))


async def s_parallel(h):
    n = len(h.workers())
    ok, evs = await h.say("同时做两件互不相关的事：一、算出 2 的 20 次方；二、列出 Python 的三种内置容器类型。各开一个对话。")
    show(evs)
    check("parallel: 没超时", ok)
    check("parallel: 开了 2 个新对话", len(h.workers()) == n + 2, f"{n} → {len(h.workers())}")
    rt = router_turns(evs, h)
    reports = [e for e in rt if e["via"] == "report"]
    check("parallel: 两份回报都到了总管", len(reports) >= 1 and sum(e["req"].count("【回报】") or 1 for e in reports) >= 1,
          f"report turns={len(reports)}")
    final = " ".join(e["reply"] or "" for e in rt[-2:])
    check("parallel: 结果里有 1048576", "1048576" in final.replace(",", "").replace(" ", ""), final[:200])


async def s_decision(h):
    n = len(h.workers())
    ok, evs = await h.say("新开一个对话：给一个记账小工具起名。先给我 A、B、C 三个候选名让我选，不要自己替我决定，等我回复。")
    show(evs)
    check("decision: 没超时", ok)
    new = [w for w in h.workers()][n:]
    w = new[0] if new else None
    check("decision: 开了新对话", w is not None)
    check("decision: 新对话在等用户定", w and w.awaiting and w.awaiting_kind == "decide", f"awaiting={w.awaiting if w else ''!r} kind={w.awaiting_kind if w else ''}")
    rt = router_turns(evs, h)
    check("decision: 总管把问题转给了用户", rt and rt[-1]["via"] == "report" and any(k in (rt[-1]["reply"] or "") for k in ("A", "选")))
    check("decision: 是结构化提问（ticket=asked）", w and w.ticket == "asked", w and w.ticket)
    check("decision: 总管收到的是【要用户定】", any("【要用户定】" in e["req"] or "要用户定" in e["req"] for e in rt), [e["req"][:40] for e in rt])
    check("decision: 总管只为这个问题跑了一轮", len([e for e in rt if e["via"] == "report"]) == 1, len([e for e in rt if e["via"] == "report"]))
    if not w:
        return
    ok, evs = await h.say("选 B")
    show(evs)
    rt = router_turns(evs, h)
    check("decision: '选 B' 转给了等你定的那个对话", any(e["num"] == w.num for e in evs) and used(rt, "send_to_session"))
    check("decision: 没再开新对话", len(h.workers()) == n + 1)


async def s_status(h):
    n = len(h.workers())
    ok, evs = await h.say("现在都有哪些对话、各自什么状态？")
    show(evs)
    rt = router_turns(evs, h)
    check("status: 用了 list_sessions", used(rt, "list_sessions"))
    check("status: 没开新对话、没派活", len(h.workers()) == n and not any(e["num"] != h.router.num for e in evs))


async def s_folder(h):
    ok, evs = await h.say("在 e2e 文件夹里新建 notes.txt，内容写一行：总管测试")
    show(evs)
    check("folder: 没超时", ok)
    check("folder: notes.txt 建在 e2e 文件夹里", (h.proj / "notes.txt").exists())


async def s_no_self_work(h):
    n = len(h.workers())
    ok, evs = await h.say("把 e2e 文件夹里 a.txt 的内容改成全大写，就这一件事，你直接改就行。")
    show(evs)
    rt = router_turns(evs, h)
    check("noself: 总管没用改文件工具", not any(str(t).startswith(k) for e in rt for t in e["tools"] for k in ("写 ", "改 ", "跑命令")))
    check("noself: 交给了对话去做", len(h.workers()) > n or used(rt, "send_to_session"))
    check("noself: a.txt 改成了大写", (h.proj / "a.txt").read_text(encoding="utf-8").strip() == "HELLO WORLD")


async def s_stop(h):
    n = len(h.workers())
    await h.b._submit(h.router, "新开一个对话：运行命令 python -c \"import time; time.sleep(150)\"，等它结束再告诉我", web(""))
    t_end = time.time() + 90
    w = None
    while time.time() < t_end and not w:
        await asyncio.sleep(2)
        new = h.workers()[n:]
        w = next((x for x in new if x.num in h.b.runner.turns), None)
    check("stop: 长任务开工了", w is not None)
    await h.settle(timeout=60, quiet=3) if not w else await asyncio.sleep(15)
    ok, evs = await h.say("把刚才那个 sleep 的对话停掉", timeout=240)
    show(evs)
    rt = router_turns(evs, h)
    check("stop: 用了 stop_session", used(rt, "stop_session"))
    check("stop: 那个对话不再运行", w and not h.b.runner.busy(w.num))


async def s_direct(h):
    w = next((x for x in h.workers() if "hello" in (x.cwd + x.name).lower() or x.num == 2), None) or h.workers()[0]
    ok, evs = await h.say(f"#{w.num} 把 hello.py 改成打印 hi，再运行一次")
    show(evs)
    check("direct: 没超时", ok)
    first = evs[0] if evs else {}
    check("direct: 先跑的是 worker，不是总管", first.get("num") == w.num and first.get("via") == "router", first.get("num"))
    rt = router_turns(evs, h)
    check("direct: 总管只在回报时跑了一轮", len(rt) == 1 and rt[0]["via"] == "report", [e["via"] for e in rt])
    check("direct: 回报那轮带着【期间的情况】", rt and rt[0]["req"].startswith("【期间的情况】"), rt and rt[0]["req"][:60])


async def s_handback(h):
    w = h.workers()[0]
    n = len(h.workers())
    ok, evs = await h.say(f"#{w.num} 帮我写一首关于秋天的五言绝句")
    show(evs)
    check("handback: 没超时", ok)
    wt = [e for e in evs if e["num"] == w.num]
    check("handback: worker 退回了", any(any("退回" in str(t) for t in e["tools"]) for e in wt), [e["tools"] for e in wt])
    rt = router_turns(evs, h)
    check("handback: 总管收到【退回】", any("退回" in e["req"] for e in rt), [e["req"][:40] for e in rt])
    check("handback: 总管另开了对话去做", len(h.workers()) > n, f"{n} → {len(h.workers())}")


async def s_humandirect(h):
    w = h.workers()[0]
    notes0 = len(h.router.notes)
    t0 = len(h.events)
    await h.b._submit(w, "把 hello.py 的内容念给我听（直接贴出来就行）", web("x"))
    ok = await h.settle()
    evs = h.events[t0:]
    show(evs)
    check("humandirect: 没超时", ok)
    check("humandirect: 总管没跑", not router_turns(evs, h))
    check("humandirect: 给总管记了一笔", len(h.router.notes) > notes0, h.router.notes[-1:] if h.router.notes else "")
    check("humandirect: 直接通知了用户", any(p[0] == w.num for p in h.pushes))


async def s_full(h):
    """一句话派 4 件，超过并行数 3：总管照样能马上回答。"""
    await h.b._submit(h.router, "同时开四个对话分别做，每个都要先运行命令 sleep 90 再回答：一、3 的 30 次方；二、100 的阶乘有多少位；"
                      "三、1 到 10000 的和；四、2 的 100 次方的各位数字之和。", web(""))
    t_end = time.time() + 180
    while time.time() < t_end and len([n for n in h.busy() if n != h.router.num]) < 3:
        await asyncio.sleep(1)
    busy = [n for n in h.busy() if n != h.router.num]
    check("full: 派出去的对话占满了并行数", len(busy) >= 3, busy)
    t_end = time.time() + 90
    while time.time() < t_end and h.b.runner.busy(h.router.num):   # 等总管派完这一轮，再单独问它
        await asyncio.sleep(1)
    busy = [n for n in h.busy() if n != h.router.num]
    check("full: 问的时候派出去的对话还占满着", len(busy) >= 3, busy)
    t = time.time()
    n0 = len(h.events)
    await h.b._submit(h.router, "现在一共有几个对话在做事？", web(""))
    while time.time() - t < 120 and not any(e["num"] == h.router.num and "几个" in e["req"] for e in h.events[n0:]):
        await asyncio.sleep(1)
    waited = time.time() - t
    check("full: 满载时总管 2 分钟内答了你", any(e["num"] == h.router.num and "几个" in e["req"] for e in h.events[n0:]), f"{waited:.0f}s")
    ok = await h.settle(timeout=400)
    show(h.events[n0:])
    check("full: 全部做完", ok)


SCENARIOS = {"dispatch": s_dispatch, "direct": s_direct, "handback": s_handback, "humandirect": s_humandirect, "full": s_full, "followup": s_followup, "parallel": s_parallel, "decision": s_decision,
             "status": s_status, "folder": s_folder, "noself": s_no_self_work, "stop": s_stop}


async def main(names):
    h = Harness()
    print(f"数据目录 {ROOT}，日志 {LOG}")
    w = None
    for name in names:
        print(f"\n=== {name} ===")
        t = time.time()
        try:
            if name == "followup":
                if not w:
                    w = await s_dispatch(h)
                await s_followup(h, w)
            elif name == "dispatch":
                w = await s_dispatch(h)
            else:
                await SCENARIOS[name](h)
        except Exception:
            traceback.print_exc()
            check(f"{name}: 没有异常", False, traceback.format_exc(limit=2))
        print(f"  （{time.time() - t:.0f}s）")
    print("\n=== 汇总 ===")
    for n, ok, d in RESULTS:
        if not ok:
            print(f"FAIL {n} {d}")
    print(f"{sum(ok for _, ok, _ in RESULTS)}/{len(RESULTS)} 通过")
    print("钉钉（不该有）：", h.dt.sent[:3])
    errs = [l for l in LOG.read_text(encoding="utf-8").splitlines() if " ERROR " in l or "Traceback" in l]
    print("日志里的 ERROR：", len(errs))
    for l in errs[:20]:
        print("  ", l[:300])
    (ROOT / "events.json").write_text(json.dumps(h.events, ensure_ascii=False, indent=1), encoding="utf-8")
    print("全部事件：", ROOT / "events.json")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or list(SCENARIOS)))
