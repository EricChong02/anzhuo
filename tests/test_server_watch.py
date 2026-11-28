import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "server_watch.py"


@pytest.fixture
def watch(tmp_path):
    spec = importlib.util.spec_from_file_location("server_watch_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.STATE = tmp_path / "state.json"
    mod.DOWN_AFTER = 3
    mod.PUSH_TRIES = 3
    mod.sent = []
    mod.online = False
    mod.push_ok = True
    mod.push_calls = 0

    def alive():
        return mod.online

    def push(title, body):
        mod.push_calls += 1
        if mod.push_ok:
            mod.sent.append(title)
        return mod.push_ok

    mod.alive, mod.push = alive, push
    return mod


def run(mod):
    mod.main()


def state(mod):
    return json.loads(mod.STATE.read_text())


def test_alerts_only_once_after_three_offline_minutes(watch):
    for _ in range(2):
        run(watch)
    assert watch.sent == []
    run(watch)
    assert watch.sent == ["远窗连不上了"]
    for _ in range(3):
        run(watch)
    assert watch.sent == ["远窗连不上了"]          # 不会每分钟重复叫
    assert state(watch)["alerted"] is True


def test_recovery_message_sent_once(watch):
    for _ in range(3):
        run(watch)
    watch.online = True
    run(watch)
    run(watch)
    assert watch.sent == ["远窗连不上了", "远窗恢复了"]
    assert state(watch) == {"fails": 0, "alerted": False, "since": 0, "tries": 0}


def test_push_failure_is_bounded_and_state_survives(watch):
    watch.push_ok = False
    for _ in range(3):
        run(watch)
    assert watch.push_calls == 1
    run(watch)
    run(watch)
    assert watch.push_calls == 3                    # 最多试 3 次
    for _ in range(3):
        run(watch)
    assert watch.push_calls == 3                    # 试够就停
    assert state(watch)["alerted"] is True


def test_state_file_remains_valid_when_push_raises(watch):
    watch.push = lambda title, body: (_ for _ in ()).throw(RuntimeError("boom"))
    for _ in range(5):
        run(watch)
    # 推送抛异常也不能让状态文件坏掉或停在“没提醒过”；失败 3 次后停下
    assert state(watch)["fails"] == 5
    assert state(watch)["alerted"] is True
