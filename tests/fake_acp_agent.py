"""测试用的假 ACP 助手（stdio 上一行一条 JSON-RPC）。按话里的关键词表演：
- 默认：想一下 → 说一句 → 跑命令 ls（要批准）→ 改 a.txt → 列计划 → 报上下文用量 → 最后回复“做完了”
- 话里有 “push”：跑 git push（要批准），被拒就回复“没推送”，批准了回复“推送了”
- 话里有 “慢”：一直等，收到 session/cancel 才结束（stopReason=cancelled）
- 话里有 “订阅”：直接报错（像 Kimi 订阅不含 Kimi Code 时那样）
收到的每个请求记在环境变量 FAKE_ACP_LOG 指定的文件里。"""
import json
import os
import sys
import threading
import time

LOG = os.environ.get("FAKE_ACP_LOG")
lock = threading.Lock()
pending: dict = {}
nid = [1000]
cancelled = threading.Event()


def send(msg):
    with lock:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def log(entry):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def update(sid, u):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": u}})


def kimi_call(cid, title, kind, args):
    """Kimi 的 tool_call：标题是工具名，参数只放在一段 JSON 文字里，没有 rawInput。"""
    return {"toolCallId": cid, "title": title, "kind": kind, "status": "pending",
            "content": [{"type": "content", "content": {"type": "text", "text": json.dumps(args, ensure_ascii=False)}}]}


def ask(sid, call, said=""):
    nid[0] += 1
    ev = threading.Event()
    pending[nid[0]] = [ev, None]
    send({"jsonrpc": "2.0", "id": nid[0], "method": "session/request_permission", "params": {
        "sessionId": sid, "toolCall": {"toolCallId": call["toolCallId"], "title": call["title"],
                                       "content": [{"type": "content", "content": {"type": "text", "text": f"Requesting approval to {said}"}}]},
        "options": [
            {"optionId": "yes", "name": "Allow", "kind": "allow_once"},
            {"optionId": "always", "name": "Always", "kind": "allow_always"},
            {"optionId": "no", "name": "Reject", "kind": "reject_once"}]}})
    ev.wait(30)
    outcome = (pending.pop(nid[0])[1] or {}).get("outcome") or {}
    return outcome.get("optionId")


def say(sid, text):
    for i in range(0, len(text), 4):
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text[i:i + 4]}})


def run_prompt(rid, sid, text):
    if "订阅" in text:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "Authentication required: subscription has no access"}})
        return
    if "慢" in text:
        say(sid, "开始慢慢做")
        cancelled.clear()
        cancelled.wait(30)
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "cancelled"}})
        return
    if "神秘" in text:   # 跑命令却什么参数都不给
        call = {"toolCallId": "m1", "title": "Bash", "kind": "execute", "status": "pending"}
        update(sid, {"sessionUpdate": "tool_call", **call})
        ok = ask(sid, call, "run something") in ("yes", "always")
        say(sid, "跑了" if ok else "没跑")
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
        return
    if "push" in text:
        call = kimi_call("p1", "Bash", "execute", {"command": "git push origin main"})
        update(sid, {"sessionUpdate": "tool_call", **call})
        ok = ask(sid, call, "Running: git push origin main") in ("yes", "always")
        say(sid, "推送了" if ok else "没推送")
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
        return
    update(sid, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "先看看目录"}})
    say(sid, "我先列一下文件。")
    call = kimi_call("c1", "Bash", "execute", {"command": "ls -la"})
    update(sid, {"sessionUpdate": "tool_call", **call})
    granted = ask(sid, call, "Running: ls -la")
    update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "title": "Running: ls -la", "status": "in_progress",
                 "rawInput": {"command": "ls -la"}, "content": [{"type": "content", "content": {"type": "text", "text": "{}"}}]})
    update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed",
                 "content": [{"type": "content", "content": {"type": "text", "text": "total 1\na.txt"}}]})
    update(sid, {"sessionUpdate": "tool_call", "toolCallId": "e1", "title": "Edit a.txt", "kind": "edit", "status": "completed",
                 "locations": [{"path": os.path.join(os.getcwd(), "a.txt")}]})
    update(sid, {"sessionUpdate": "plan", "entries": [{"content": "列文件", "priority": "high", "status": "completed"},
                                                      {"content": "改 a.txt", "priority": "high", "status": "in_progress"}]})
    update(sid, {"sessionUpdate": "usage_update", "used": 25000, "size": 100000})
    say(sid, f"做完了（批准：{granted}）")
    send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})


def main():
    sys.stdin.reconfigure(encoding="utf-8")   # Windows 上默认按 GBK 读，ACP 是 UTF-8
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" in msg and ("result" in msg or "error" in msg) and msg["id"] in pending:
            pending[msg["id"]][1] = msg.get("result") or {}
            pending[msg["id"]][0].set()
            continue
        method, params, rid = msg.get("method"), msg.get("params") or {}, msg.get("id")
        log({"method": method, "params": params})
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1, "agentCapabilities": {
                "loadSession": True, "sessionCapabilities": {"resume": {}, "additionalDirectories": {}}}}})
        elif method in ("session/new", "session/resume"):
            sid = params.get("sessionId") or f"s-{int(time.time() * 1000)}"
            send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": sid, "configOptions": [
                {"type": "select", "id": "model", "category": "model", "currentValue": "fake-1",
                 "options": [{"value": "fake-1", "name": "Fake 1"}, {"value": "fake-2", "name": "Fake 2"}]},
                {"type": "select", "id": "thinking", "category": "thought_level", "currentValue": "on",
                 "options": [{"value": "off", "name": "Off"}, {"value": "on", "name": "On"}]}]}})
        elif method == "session/set_config_option":
            send({"jsonrpc": "2.0", "id": rid, "result": {"configOptions": []}})
        elif method == "session/prompt":
            text = "".join(b.get("text", "") for b in params.get("prompt") or [] if b.get("type") == "text")
            threading.Thread(target=run_prompt, args=(rid, params["sessionId"], text), daemon=True).start()
        elif method == "session/cancel":
            cancelled.set()
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "not found"}})


if __name__ == "__main__":
    main()
