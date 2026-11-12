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
