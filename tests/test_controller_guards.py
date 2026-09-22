import json
from pathlib import Path

from dev_harness.config import Task
from dev_harness.controller import _prompt, _valid_result, _verification_valid
from dev_harness.verify import code_hash


def task() -> Task:
    return Task(
        name="sample",
        requirements=("implement",),
        edit_scope=("src",),
        acceptance_criteria=("passes",),
        verification_commands=(("python", "check.py"),),
    )


def evidence(root: Path) -> dict:
    return {
        "code_hash": code_hash(root),
        "end_code_hash": code_hash(root),
        "commands": [
            {
                "argv": ["python", "check.py"],
                "exit_code": 0,
                "timed_out": False,
                "stdout": "ok",
                "stderr": "",
            }
        ],
        "success": True,
    }


def test_verification_requires_exact_commands_and_current_code(tmp_path: Path) -> None:
    (tmp_path / "check.py").write_text("print('ok')", encoding="utf-8")
    record = evidence(tmp_path)
    assert _verification_valid(record, task(), tmp_path)

    stale = json.loads(json.dumps(record))
    (tmp_path / "check.py").write_text("print('changed')", encoding="utf-8")
    assert not _verification_valid(stale, task(), tmp_path)

    current = evidence(tmp_path)
    current["commands"][0]["argv"] = ["python", "different.py"]
    assert not _verification_valid(current, task(), tmp_path)


def test_phase_result_validation_is_fail_closed() -> None:
    valid = {"status": "PASS", "summary": "ok", "issues": [], "review_findings": []}
    assert _valid_result(valid)
    assert not _valid_result({"status": "PASS"})
    assert not _valid_result({**valid, "summary": 1})
    assert not _valid_result(
        {
            **valid,
            "review_findings": [{"id": "x", "severity": "P9", "description": "bad"}],
        }
    )


def test_every_phase_prompt_requires_blocked_instead_of_guessing() -> None:
    for phase in ("実装", "テスト", "修正", "レビュー"):
        prompt = _prompt(phase, task(), [], "verify-command", {})
        assert "不明点や矛盾" in prompt
        assert "BLOCKED" in prompt
