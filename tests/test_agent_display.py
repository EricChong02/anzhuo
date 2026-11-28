import json
from pathlib import Path
import shutil
import subprocess

from claude_agent_sdk import ToolUseBlock
from claude_agent_sdk.types import TaskStartedMessage, TaskNotificationMessage
from anzhuo.engines.claude import remember_agent_tool, track_agent, running_agents
from anzhuo.report import Turn, live_agents


def started(task, tool, description, task_type=None):
    return TaskStartedMessage(subtype="task_started", data={}, task_id=task, description=description,
                              uuid="u", session_id="s", tool_use_id=tool, task_type=task_type)


def test_six_named_agents_and_commands_keep_identity_and_model():
    turn = Turn(started=1)
    for i in range(6):
        remember_agent_tool(turn, ToolUseBlock(id=f"tu{i}", name="Agent", input={"name": f"助手{i}", "description": "检查课程", "model": "opus"}), "claude-sonnet-5-5")
        track_agent(turn, started(f"a{i}", f"tu{i}", "检查课程"))
    for i in range(10):
        remember_agent_tool(turn, ToolUseBlock(id=f"bash{i}", name="Bash", input={"description": "Wait for acceptance"}))
        track_agent(turn, started(f"b{i}", f"bash{i}", "Wait for acceptance"))
    rows = live_agents(turn)
    assert len(rows) == 16 and len(running_agents(turn)) == 6
    agents = [a for a in rows if a["kind"] == "助手"]
    assert [a["name"] for a in agents] == [f"助手{i}" for i in range(6)]
    assert all(a["model"] == "Opus 5.5" for a in agents)
    track_agent(turn, TaskNotificationMessage(subtype="task_notification", data={}, task_id="a0", status="completed", output_file="", summary="", uuid="u", session_id="s"))
    assert len(live_agents(turn)) == 15 and len(running_agents(turn)) == 5


def test_missing_model_is_not_invented_and_inherit_uses_configuration():
    turn = Turn(started=1)
    track_agent(turn, started("unknown", "u", "未报告型号的助手"))
    assert not live_agents(turn)[0]["model"]
    remember_agent_tool(turn, ToolUseBlock(id="u", name="Agent", input={"name": "后到的名称", "model": "inherit"}), "claude-sonnet-5-5")
    row = live_agents(turn)[0]
    assert row["name"] == "后到的名称" and row["model"] == "Sonnet 5.5"


def test_frontend_shows_all_six_agents_and_separate_commands():
    import anzhuo
    source = (Path(anzhuo.__file__).parent / "static/app/core.js").read_text()
    function = source[source.index("function agentsHTML("):source.index("function procHTML(")]
    rows = [{"name": f"助手{i}", "kind": "助手", "status": "running", "model": "Opus 5.5", "secs": 5} for i in range(6)]
    rows += [{"name": "Wait for acceptance", "kind": "命令", "status": "running", "secs": 2} for _ in range(10)]
    program = "const esc=s=>String(s??'').replaceAll('<','&lt;');const dur=s=>s+'秒';" + function
    program += f"const rows = {json.dumps(rows)}; console.log(JSON.stringify([agentsHTML(rows), agentsHTML(rows, true)]));"
    html, expanded = json.loads(subprocess.check_output([shutil.which("node"), "-e", program], text=True))
    assert "6 个助手在跑" in html and "10 条后台命令在运行" in html
    assert html.count("Opus 5.5") == 6
    assert all(f"助手{i}" in html for i in range(6))
    assert '<details class="agents command-jobs">' in html
    assert '<details class="agents command-jobs" open>' in expanded


def test_actual_stream_keeps_parent_model_and_observes_child_model():
    import asyncio
    from claude_agent_sdk import AssistantMessage, TextBlock
    from anzhuo.engines import CLAUDE
    from anzhuo.sessions import Session, Job

    turn = Turn(started=1)
    class Client:
        async def query(self, prompt):
            pass
        async def receive_messages(self):
            yield AssistantMessage(content=[ToolUseBlock(id="tu", name="Agent", input={"name": "排版助手"})], model="claude-sonnet-5-5")
            yield started("child", "tu", "检查排版")
            yield AssistantMessage(content=[TextBlock(text="核对完成")], model="claude-opus-5-5", parent_tool_use_id="tu")
    asyncio.run(CLAUDE._read(Client(), Session(num=1, name="测试", cwd="/tmp"), Job(text="只做测试"), turn, "claude-haiku-4-5"))
    assert turn.model == "claude-sonnet-5-5"
    row = live_agents(turn)[0]
    assert row["name"] == "排版助手" and row["model"] == "Opus 5.5" and row["model_source"] == "observed"
