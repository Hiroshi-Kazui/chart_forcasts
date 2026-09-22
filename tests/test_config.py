import json
from pathlib import Path

import pytest

from claude_harness.config import Task, TaskError


def write_task(path: Path, **overrides: object) -> Path:
    payload: dict[str, object] = {
        "name": "sample",
        "requirements": ["implement it"],
        "edit_scope": ["src"],
        "acceptance_criteria": ["tests pass"],
        "verification_commands": [["python", "-m", "pytest"]],
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "overrides",
    [
        {"unknown": True},
        {"requirements": []},
        {"requirements": [1]},
        {"edit_scope": ["../escape"]},
        {"edit_scope": [str(Path.cwd().anchor or "C:\\") + "escape"]},
        {"requirement_files": ["../secret"]},
        {"requirement_files": ["/outside.txt"]},
        {"requirement_files": ["D:outside.txt"]},
        {"exclude": ["../outside"]},
        {"verification_commands": []},
        {"verification_commands": [["python", 1]]},
        {"timeouts": {"verification": 0}},
        {"timeouts": {"delivery": 14401}},
        {"timeouts": {"verification": 1801}},
        {"timeouts": {"total": 14401}},
        {"timeouts": {"fix": 100}},
    ],
)
def test_task_rejects_values_outside_frozen_contract(tmp_path: Path, overrides: dict) -> None:
    with pytest.raises(TaskError):
        Task.load(write_task(tmp_path / "task.json", **overrides))


def test_task_accepts_lower_timeouts(tmp_path: Path) -> None:
    task = Task.load(
        write_task(
            tmp_path / "task.json",
            timeouts={"delivery": 5, "verification": 2, "total": 10},
        )
    )
    assert task.timeouts.delivery == 5
    assert task.timeouts.total == 10
