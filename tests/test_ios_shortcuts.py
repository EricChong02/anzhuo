"""回归：条件比较文本须采用原生封装，否则 iPhone 提示参数未填写。"""
import importlib.util
from pathlib import Path
import pytest

@pytest.mark.parametrize("mode", ["share", "screenshot"])
def test_condition_operands_use_native_parameter_format(mode):
    path = Path(__file__).resolve().parents[1] / "scripts/make_ios_shortcuts.py"
    spec = importlib.util.spec_from_file_location("ios_generator", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    data = mod.build("https://example.test", "test-only-token", mode)
    conditions = [x["WFWorkflowActionParameters"] for x in data["WFWorkflowActions"]
                  if x["WFWorkflowActionIdentifier"].endswith(".conditional")
                  and x["WFWorkflowActionParameters"]["WFControlFlowMode"] == 0]
    assert len(conditions) == 3
    for item in conditions:
        if "WFConditionalActionString" in item:
            operand = item["WFConditionalActionString"]
            assert isinstance(operand, dict), "裸字符串在 iPhone 中会显示为空比较值"
            assert operand["WFSerializationType"] == "WFTextTokenString"
            assert operand["Value"]["string"] == "新开对话"
            expected = "WFStringContentItem"
        else:
            expected = "WFNumberContentItem"
        value = item["WFInput"]["Variable"]["Value"]
        assert value["Aggrandizements"][0]["CoercionItemClass"] == expected


@pytest.mark.parametrize("mode", ["launch", "share", "screenshot", "voice"])
def test_shortcuts_open_installed_app_not_safari(mode):
    path = Path(__file__).resolve().parents[1] / "scripts/make_ios_shortcuts.py"
    spec = importlib.util.spec_from_file_location("ios_desktop_generator", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    data = mod.build("https://example.test", "test-only-token", mode)
    actions = data["WFWorkflowActions"]
    by_id = {x["WFWorkflowActionParameters"].get("UUID"): x for x in actions}
    opens = [x for x in actions if x["WFWorkflowActionIdentifier"].endswith(".openurl")]
    assert len(opens) == 1
    ref = opens[0]["WFWorkflowActionParameters"]["WFInput"]["Value"]["OutputUUID"]
    source = by_id[ref]
    assert source["WFWorkflowActionIdentifier"] == "is.workflow.actions.url"
    url = source["WFWorkflowActionParameters"]["WFURLActionURL"]
    if mode == "launch":
        assert url == "webapp://example.test/_x/#new"
    else:
        assert url["WFSerializationType"] == "WFTextTokenString"
        value = url["Value"]
        assert value["string"] == "webapp://example.test/_x/#s\ufffc"
        attachment = next(iter(value["attachmentsByRange"].values()))
        source = by_id[attachment["OutputUUID"]]
        assert source["WFWorkflowActionParameters"]["WFDictionaryKey"] == "num"
        assert all(x["WFWorkflowActionParameters"].get("WFDictionaryKey") != "url" for x in actions)
    for action in actions:
        if action["WFWorkflowActionIdentifier"].endswith(".downloadurl"):
            assert action["WFWorkflowActionParameters"]["WFURL"].startswith("https://example.test/_x/q/")
    assert mod.desktop_url("https://example.test/prefix", "new") == "webapp://example.test/prefix/_x/#new"
