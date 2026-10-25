
from remote_claude import folders, report
from remote_claude.commands import parse
from remote_claude.config import Config
from remote_claude.dingtalk import parse_incoming


def test_parse_commands():
    c = parse("新开 课程根 平台：做第2课小节3")
    assert (c.kind, c.folders, c.text) == ("new", ["课程根", "平台"], "做第2课小节3")
    c = parse("新开：查一下天气")
    assert (c.kind, c.folders, c.text) == ("new", [], "查一下天气")
    c = parse("新开 课程根+平台:做")
    assert c.folders == ["课程根", "平台"]
    c = parse("新建 personal/菜谱整理：整理菜谱")
    assert (c.kind, c.folders, c.text) == ("mkdir", ["personal/菜谱整理"], "整理菜谱")
    assert parse("切到 #7").num == 7
    assert parse("停").kind == "stop" and parse("停").num is None
    assert parse("停 3").num == 3
    assert parse("2").kind == "pick"
    c = parse("#5 标题再大一点")
    assert (c.kind, c.num, c.text) == ("message", 5, "标题再大一点")
    assert parse("停一下，先别改").kind == "message"
    assert parse("接着 课程根 2").kind == "resume"
    c = parse("模型 7 sonnet")
    assert (c.kind, c.num, c.name) == ("model", 7, "sonnet")
    c = parse("深度 高")
    assert (c.kind, c.num, c.name) == ("effort", None, "高")
    assert parse("额度").kind == "quota"
    assert (parse("删 7").kind, parse("删 #7").num) == ("delete", 7)
    assert parse("删掉第二段").kind == "message"
    assert (parse("接着桌面").kind, parse("接着桌面").num, parse("接着桌面 2").num) == ("desktop", None, 2)
    assert parse("接着 课程根 2").kind == "resume"


def test_folder_candidates(tmp_path):
    (tmp_path / "a" / "菜谱整理").mkdir(parents=True)
    (tmp_path / "b" / "旧菜谱").mkdir(parents=True)
    (tmp_path / "_原始私密" / "菜谱秘密").mkdir(parents=True)
    (tmp_path / "c" / "node_modules" / "菜谱包").mkdir(parents=True)
    cfg = Config(client_id="x", allowed_user_ids=[], folders={"根": str(tmp_path / "a")},
                 search_roots=[str(tmp_path)], scratch_root=str(tmp_path / "s"))
    assert folders.candidates("根", cfg) == [tmp_path / "a"]
    got = folders.candidates("菜谱", cfg)
    assert got[0].name == "菜谱整理" and {p.name for p in got} == {"菜谱整理", "旧菜谱"}
    assert folders.candidates("不存在的名字", cfg) == []
    assert folders.candidates(str(tmp_path / "b"), cfg) == [tmp_path / "b"]
    assert folders.split_new_path("personal/菜谱整理") == ("personal", "菜谱整理")
    assert folders.scratch_dir(cfg, 3).is_dir()


def test_parse_incoming():
    d = {"msgtype": "text", "text": {"content": " 你好 ", "isReplyMsg": True,
                                     "repliedMsg": {"content": {"text": "【做完了】#7 第2课"}}},
         "senderStaffId": "u1", "conversationType": "1"}
    inc = parse_incoming(d)
    assert inc.text == "你好" and "#7" in inc.quoted_text
    d = {"msgtype": "richText", "content": {"richText": [{"text": "看这里"}, {"type": "picture", "downloadCode": "c1"}]},
         "senderStaffId": "u1", "conversationType": "1"}
    inc = parse_incoming(d)
    assert inc.text == "看这里" and inc.image_codes == ["c1"]
    inc = parse_incoming({"msgtype": "audio", "content": {"recognition": "选B"}, "conversationType": "1"})
    assert inc.text == "选B"


def test_report():
    t = report.Turn(started=0, ended=300, request="把标题改大", changed_files=["/w/a.py", "/w/b.py"], commands=["ls"],
                    final_text="要你做的：没有\n做了什么：改了两处", model="claude-opus-5-5", effort="high",
                    context_pct=75, compacted=True)
    title, body = report.build("#3 测试", t, "/w", {"five_hour": (0.85, "10月25日 17:10"), "seven_day": (0.4, None)})
    assert title == "做完了 · 把标题改大" and "#3 · 5 分钟 · 改 2 个文件 · 跑 1 条命令" in body and "a.py" in body
    assert "要你做的" not in body and "**做了什么**：改了两处" in body and "Opus 5.5 · 深度高" in body
    assert "| 上下文 | ■■■■□ 75% |" in body and "| 5 小时 | ■■■■□ 85% |" in body and "| 本周 | ■■□□□ 40% |" in body
    assert body.count("**注意**") == 3 and "<font" not in body and "　" not in body
    chat = report.Turn(started=0, ended=10, request="你好", final_text="你好！有什么要帮忙的？")
    ct, cb = report.build("#3 测试", chat, "/w")
    assert ct == "回复 · 你好" and "【" not in cb and cb.startswith("你好！")
    t.final_text = "要你定：标题放哪？"
    assert report.build("#3 测试", t, "/w")[0].startswith("等你定")
    t.is_error, t.error = True, "登录失效"
    assert "登录失效" in report.build("#3 测试", t, "/w")[1]
    parts = report.split("段" * 5000 + "\n\n" + "尾", limit=3500)
    assert len(parts) >= 2 and all(len(p) <= 3500 for p in parts)


def test_brief():
    assert report.brief("今天是几号？顺便看一眼桌面上有几个文件") == "今天是几号？"
    assert report.brief("把第二课第三小节的标题改大一点并且换个颜色看看效果", 16) == "把第二课第三小节的标题改大一点并…"
    assert report.brief("") == ""


def test_parse_usage():
    from remote_claude.sessions import parse_usage
    text = """Current session: 15% used · resets Oct 25 at 5:10pm (Asia/Taipei)
Current week (all models): 20% used · resets Oct 31 at 4am (Asia/Taipei)
Current week (Fable): 0% used · resets Oct 31 at 4am (Asia/Taipei)"""
    got = parse_usage(text)
    assert got == {"five_hour": (0.15, "10月25日 17:10"), "seven_day": (0.2, "10月31日 04:00")}


def test_is_reply():
    from remote_claude.app import _is_reply
    for t in ["好的", "选B", "继续", "再大一点", "嗯嗯", "2", "为什么？"]:
        assert _is_reply(t), t
    for t in ["帮我查一下明天厦门天气", "把桌面上的文件按日期列一下", "好的，那顺便把第三课也改了"]:
        assert not _is_reply(t), t


def test_empty_sections_hidden():
    text = "答案是 3。\n要你做的：没有。\n做了什么：\n1. 查了日期\n改了哪些：没有改任何东西，只是查看。\n自查结果：日期取自本机。\n截图：无。"
    out = report.bold_sections(text)
    assert "要你做的" not in out and "改了哪些" not in out and "截图" not in out
    assert "**做了什么**" in out and "1. 查了日期" in out and "**自查结果**：日期取自本机。" in out


def test_model_choice(tmp_path):
    from remote_claude.app import _looks_like_project, inline_overrides
    assert inline_overrides("这次用 Opus，深度调到高") == ("claude-opus-5-5", "high")
    assert inline_overrides("帮我查天气") == (None, None)
    assert inline_overrides("思考最高，改用sonnet") == ("claude-sonnet-5-5", "max")
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "repo" / "src").mkdir()
    assert _looks_like_project(tmp_path / "repo" / "src") and not _looks_like_project(tmp_path)


def test_router_parse():
    from remote_claude.router import _parse
    r = _parse('{"action":"reply","answer":"你好！","target":null}', {1})
    assert (r.action, r.answer) == ("reply", "你好！")
    assert _parse('{"action":"reply","answer":""}', set()).action == "ask"
    r = _parse('好的 {"action":"new","folder":"课程根","kind":"dev"}', {1})
    assert (r.action, r.folder, r.kind) == ("new", "课程根", "dev")
    assert _parse('{"action":"continue","target":9}', {1}).action == "ask"
    assert _parse("不是 JSON", {1}) is None


def test_needs_translation():
    from remote_claude.translate import needs_translation
    assert needs_translation("I updated the cover title and checked that it still fits on the lab screen.")
    assert not needs_translation("改好了第二课封面，用 Opus 5.5 跑了 course_gates.py G1，结果 PASS。")
    assert not needs_translation("跑了：\n```\nuv run pytest -q tests/test_units.py --maxfail one two three four\n```")
    assert not needs_translation("路径是 ~/Projects/04_项目/work/CyberScope/Frontend/courses/_shared/lesson-shell.js")
    assert needs_translation("做完了。\n\nNote: the preview server was already running, so I reused it instead of starting another.")


def test_translated_footer():
    t = report.Turn(started=0, ended=60, request="改标题", final_text="改好了", translated="DeepSeek",
                    changed_files=["/w/a.html"])
    assert "英文已由 DeepSeek 译成中文" in report.build("#1 测试", t, "/w")[1]


def test_cards():
    import json
    from remote_claude import cards
    assert cards.step_label("Read", {"file_path": "/a/b/app.py"}) == "看 app.py"
    assert cards.step_label("Bash", {"command": "pytest -q", "description": "跑测试"}) == "跑命令：跑测试"
    assert cards.step_label("ToolSearch", {}) is None
    text = """要你定：封面标题用哪种字号？

A. 64px，最醒目
**B.** 56px，和正文更协调
- C、保持 48px

推荐：B，机房屏上不挤"""
    d = cards.parse_decision(text)
    assert d.question == "封面标题用哪种字号？"
    assert [k for k, _ in d.options] == ["A", "B", "C"] and d.options[1][1] == "56px，和正文更协调"
    assert d.recommend == "B"
    assert cards.parse_decision("要你定：只有一个 A. 方案") is None
    assert cards.parse_decision("做完了。A. 一 B. 二") is None
    data = cards.decision_data("#2 封面", d)
    btns = json.loads(data["sys_full_json_obj"])["msgButtons"]
    assert [b["id"] for b in btns] == ["pick:A", "pick:B", "pick:C"] and btns[1]["text"] == "选 B（推荐）"
    picked = cards.decision_data("#2 封面", d, picked="B")
    assert json.loads(picked["sys_full_json_obj"])["msgButtons"] == [] and "你选了 B" in picked["markdown"]
    assert cards.action_of('{"cardPrivateData": {"actionIds": ["pick:A"], "params": {}}}') == "pick:A"
    assert cards.action_of({"whatever": {"id": "stop"}}) == "stop"
    nested = {"outTrackId": "d4-1", "content": '{"cardPrivateData":{"actionIds":["single_button_node_x"],"params":{"id":"pick:B"}}}'}
    assert cards.action_of(nested) == "pick:B"
    assert cards.action_of({"content": '{"cardPrivateData":{"actionIds":["single_button_node_x"]}}'}) is None
    t = report.Turn(started=0, request="改封面", steps=[f"看 {i}.py" for i in range(9)])
    p = cards.progress_data("#2 封面", t)
    assert "4. 看 3.py" in p["markdown"] and "9. 看 8.py" in p["markdown"] and "看 2.py" not in p["markdown"]
    assert json.loads(p["sys_full_json_obj"])["msgButtons"][0]["id"] == "stop"
    t.ended = 120
    f = cards.progress_data("#2 封面", t, final="做完了")
    assert f["title"].startswith("做完了") and "共 9 步" in f["markdown"]
