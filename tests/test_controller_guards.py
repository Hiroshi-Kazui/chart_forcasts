from pathlib import Path

from claude_harness.config import Task
from claude_harness.controller import _outside, _valid_result
from codex_harness.prompts import build_prompt


def task() -> Task:
    return Task(
        name="sample",
        requirements=("implement",),
        edit_scope=("src",),
        acceptance_criteria=("passes",),
        verification_commands=(("python", "check.py"),),
    )


def test_delivery_result_validation_is_fail_closed() -> None:
    valid = {"status": "PASS", "summary": "ok", "issues": []}
    assert _valid_result(valid)
    assert not _valid_result(None)
    assert not _valid_result({"status": "PASS"})
    assert not _valid_result({**valid, "summary": 1})
    assert not _valid_result({**valid, "status": "DONE"})
    assert not _valid_result({**valid, "issues": [1]})


def test_changes_outside_edit_scope_are_listed() -> None:
    base = {"src/a.py": "1", "other.py": "1"}
    after = {"src/a.py": "2", "other.py": "2"}
    assert _outside(base, after, ("src",)) == ["other.py"]
    assert _outside(base, after, ("src", "other.py")) == []


def test_order_prompt_leaves_process_to_the_contractor(tmp_path: Path) -> None:
    prompt = build_prompt(task(), {})
    assert "受注側の裁量" in prompt
    assert "不明点や矛盾" in prompt
    assert "BLOCKED" in prompt
    assert "edit_scope" in prompt
    for word in ("実装工程", "テスト担当", "レビュー担当", "修正回数"):
        assert word not in prompt
