from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
TERMINAL = {"COMPLETED", "FAILED", "BLOCKED", "STOPPED"}
AWAITING = "AWAITING_FINAL_REVIEW"


def _env(tmp_path: Path, scenario: str) -> dict[str, str]:
    launcher = tmp_path / "fake-codex.cmd"
    launcher.write_text(f'@"{sys.executable}" "{REPO / "tests" / "fake_codex.py"}" %*\r\n')
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO / ".claude"), str(REPO / ".codex")])
    env["DEV_HARNESS_CODEX"] = str(launcher)
    env["DEV_HARNESS_TEST_SCENARIO"] = scenario
    return env


def _task(project: Path, *, requirement_files: list[str] | None = None) -> Path:
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.txt").write_text("old\n", encoding="utf-8")
    task = {
        "name": "tiny",
        "requirements": ["write ok to src/app.txt"],
        "edit_scope": ["src"],
        "acceptance_criteria": ["file contains ok"],
        "verification_commands": [
            [
                sys.executable,
                "-c",
                "from pathlib import Path; assert Path('src/app.txt').read_text() == 'ok\\n'",
            ]
        ],
        "timeouts": {"delivery": 10, "verification": 10, "total": 40},
    }
    if requirement_files is not None:
        task["requirement_files"] = requirement_files
    path = project / "task.json"
    path.write_text(json.dumps(task), encoding="utf-8")
    return path


def _cli(project: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        creationflags = subprocess.CREATE_NO_WINDOW
    return subprocess.run(
        [sys.executable, "-m", "claude_harness", *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        startupinfo=startupinfo,
        creationflags=creationflags,
    )


def _status(project: Path, env: dict[str, str], run_id: str) -> dict:
    result = _cli(project, env, "status", "--run", run_id, "--json")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _launch(project: Path, scenario: str, **kwargs) -> tuple[str, dict[str, str]]:
    env = _env(project, scenario)
    launched = _cli(project, env, "run", "--task", str(_task(project, **kwargs)))
    assert launched.returncode == 0, launched.stderr
    return launched.stdout.strip(), env


def _write_verdict(
    project: Path,
    run_id: str,
    *,
    status: str = "PASS",
    findings: list[dict] | None = None,
    model: str = "claude-fable-5-1",
    name: str = "verdict.json",
) -> Path:
    path = project / ".harness" / "runs" / run_id / name
    path.write_text(
        json.dumps(
            {
                "status": status,
                "summary": "最終レビュー判定",
                "findings": findings if findings is not None else [],
                "reviewer": {"model": model, "session": "pytest"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _finding(project: Path, run_id: str, severity: str = "P1") -> dict:
    return {
        "id": "F1",
        "severity": severity,
        "description": "最終レビューの必須指摘",
        "evidence": str(project / ".harness" / "runs" / run_id / "verification.json"),
    }


def _await(project: Path, env: dict[str, str], run_id: str, *, timeout: float = 40) -> dict:
    """最終レビュー待ちか終端状態まで進める。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = _status(project, env, run_id)
        if record["status"] == AWAITING or record["status"] in TERMINAL:
            return record
        time.sleep(0.2)
    pytest.fail(f"run {run_id} did not settle within {timeout} seconds")


def _accept(project: Path, env: dict[str, str], run_id: str, *, timeout: float = 25) -> dict:
    path = _write_verdict(project, run_id)
    submitted = _cli(project, env, "final-review", "--run", run_id, "--file", str(path))
    assert submitted.returncode == 0, submitted.stderr
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = _status(project, env, run_id)
        if record["status"] in TERMINAL:
            return record
        time.sleep(0.2)
    pytest.fail(f"run {run_id} did not finish applying within {timeout} seconds")


def _excluded_seconds(project: Path, run_id: str) -> float:
    with sqlite3.connect(project / ".harness" / "state.sqlite3") as db:
        row = db.execute("SELECT excluded_seconds FROM runs WHERE id=?", (run_id,)).fetchone()
    return float(row[0])


def test_single_delivery_is_verified_then_held_for_final_review(tmp_path: Path) -> None:
    run_id, env = _launch(tmp_path, "normal")
    record = _await(tmp_path, env, run_id)
    assert record["status"] == AWAITING, record
    assert record["phase"] == "最終レビュー"
    assert record["controller_pid"] is None
    assert (tmp_path / "src" / "app.txt").read_text(encoding="utf-8") == "old\n"

    run_dir = tmp_path / ".harness" / "runs" / run_id
    deliveries = sorted((run_dir / "deliveries").glob("*"))
    assert [p.name for p in deliveries] == ["01"]
    invocation = json.loads((deliveries[0] / "invocation.json").read_text(encoding="utf-8"))
    assert invocation["model"] == "gpt-5.6-sol"
    verification = json.loads((run_dir / "verification.json").read_text(encoding="utf-8"))
    assert verification["success"] is True
    request = json.loads((run_dir / "final-review-request.json").read_text(encoding="utf-8"))
    assert sorted(request["changed_files"]) == ["src/app.txt", "src/test_app.py"]


def test_fixed_verification_is_run_by_the_client_and_can_reject_a_delivery(tmp_path: Path) -> None:
    run_id, env = _launch(tmp_path, "verification-fail")
    record = _await(tmp_path, env, run_id)
    assert record["status"] == "FAILED", record
    assert (tmp_path / "src" / "app.txt").read_text(encoding="utf-8") == "old\n"
    run_dir = tmp_path / ".harness" / "runs" / run_id
    verification = json.loads((run_dir / "verification.json").read_text(encoding="utf-8"))
    assert verification["success"] is False
    assert verification["commands"][0]["exit_code"] != 0
    assert not (run_dir / "final-review-request.json").exists()


@pytest.mark.parametrize(
    "scenario", ["scope-violation", "schema-invalid", "self-report-fail", "mutate-requirement"]
)
def test_defective_delivery_fails_closed(tmp_path: Path, scenario: str) -> None:
    kwargs = {}
    if scenario == "mutate-requirement":
        (tmp_path / "requirements.md").write_text("original requirement", encoding="utf-8")
        kwargs = {"requirement_files": ["requirements.md"]}
    run_id, env = _launch(tmp_path, scenario, **kwargs)
    record = _await(tmp_path, env, run_id)
    assert record["status"] == "FAILED", record
    assert (tmp_path / "src" / "app.txt").read_text(encoding="utf-8") == "old\n"
    if scenario == "mutate-requirement":
        assert (tmp_path / "requirements.md").read_text(encoding="utf-8") == "original requirement"


def test_blocked_delivery_can_be_resumed(tmp_path: Path) -> None:
    run_id, env = _launch(tmp_path, "blocked")
    record = _await(tmp_path, env, run_id)
    assert record["status"] == "BLOCKED", record
    assert "要件が不足" in record["message"]
    assert _cli(tmp_path, env, "resume", "--run", run_id).returncode == 0


def test_stop_then_resume_reorders_and_completes(tmp_path: Path) -> None:
    run_id, env = _launch(tmp_path, "slow")
    run_dir = tmp_path / ".harness" / "runs" / run_id
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not list((run_dir / "deliveries").glob("01/result.json")):
        time.sleep(0.1)
    assert _cli(tmp_path, env, "stop", "--run", run_id).returncode == 0
    while time.monotonic() < deadline:
        record = _status(tmp_path, env, run_id)
        if record["status"] == "STOPPED":
            break
        time.sleep(0.1)
    assert record["status"] == "STOPPED", record
    created = record["created"]
    assert _cli(tmp_path, env, "resume", "--run", run_id).returncode == 0
    record = _await(tmp_path, env, run_id)
    assert record["status"] == AWAITING, record
    assert record["created"] == created
    assert sorted(p.name for p in (run_dir / "deliveries").glob("*")) == ["01", "02"]
    first = json.loads((run_dir / "deliveries" / "01" / "invocation.json").read_text("utf-8"))
    second = json.loads((run_dir / "deliveries" / "02" / "invocation.json").read_text("utf-8"))
    assert second["timeout"] < first["timeout"]


def test_dead_controller_is_reconciled_and_run_can_resume(tmp_path: Path) -> None:
    run_id, env = _launch(tmp_path, "slow")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        record = _status(tmp_path, env, run_id)
        if record["status"] == "RUNNING" and record["controller_pid"]:
            break
        time.sleep(0.1)
    assert record["status"] == "RUNNING", record
    os.kill(int(record["controller_pid"]), signal.SIGTERM)
    while time.monotonic() < deadline:
        record = _status(tmp_path, env, run_id)
        if record["status"] != "RUNNING":
            break
        time.sleep(0.2)
    assert record["status"] in {"FAILED", "STOPPED", "BLOCKED"}, record
    assert _cli(tmp_path, env, "resume", "--run", run_id).returncode == 0
    _cli(tmp_path, env, "stop", "--run", run_id)


def test_wait_returns_when_claude_has_to_decide(tmp_path: Path) -> None:
    run_id, env = _launch(tmp_path, "normal")
    waited = _cli(tmp_path, env, "wait", "--run", run_id, "--json", "--timeout", "12")
    assert waited.returncode == 0, waited.stderr
    assert json.loads(waited.stdout)["status"] == AWAITING


def test_final_review_pass_applies_changes_and_records_verdict(tmp_path: Path) -> None:
    run_id, env = _launch(tmp_path, "normal")
    assert _await(tmp_path, env, run_id)["status"] == AWAITING
    record = _accept(tmp_path, env, run_id)
    assert record["status"] == "COMPLETED", record
    assert (tmp_path / "src" / "app.txt").read_text(encoding="utf-8") == "ok\n"

    run_dir = tmp_path / ".harness" / "runs" / run_id
    assert sorted(json.loads((run_dir / "changes.json").read_text("utf-8"))) == [
        "src/app.txt",
        "src/test_app.py",
    ]
    verdict = json.loads((run_dir / "final-review.json").read_text(encoding="utf-8"))
    assert verdict["reviewer"]["model"] == "claude-fable-5-1"
    decisions = (run_dir / "control-decisions.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["phase"] for line in decisions] == [
        "発注",
        "検証",
        "最終レビュー",
        "反映",
    ]


@pytest.mark.parametrize("mutate", ["schema", "model", "evidence", "pass-with-blocking"])
def test_final_review_rejects_untrustworthy_verdict(tmp_path: Path, mutate: str) -> None:
    run_id, env = _launch(tmp_path, "normal")
    assert _await(tmp_path, env, run_id)["status"] == AWAITING
    run_dir = tmp_path / ".harness" / "runs" / run_id
    path = run_dir / "bad-verdict.json"
    if mutate == "schema":
        path.write_text(json.dumps({"status": "PASS"}), encoding="utf-8")
    elif mutate == "model":
        path = _write_verdict(tmp_path, run_id, model="gpt-6-astra", name="bad-verdict.json")
    elif mutate == "evidence":
        outside = dict(_finding(tmp_path, run_id))
        outside["evidence"] = str(tmp_path / "src" / "app.txt")
        path = _write_verdict(
            tmp_path, run_id, status="FAIL", findings=[outside], name="bad-verdict.json"
        )
    else:
        path = _write_verdict(
            tmp_path,
            run_id,
            status="PASS",
            findings=[_finding(tmp_path, run_id)],
            name="bad-verdict.json",
        )
    rejected = _cli(tmp_path, env, "final-review", "--run", run_id, "--file", str(path))
    assert rejected.returncode == 2, rejected.stdout
    assert _status(tmp_path, env, run_id)["status"] == AWAITING
    assert not (run_dir / "final-review.json").exists()
    assert (tmp_path / "src" / "app.txt").read_text(encoding="utf-8") == "old\n"


def test_final_review_rejects_verdict_when_work_copy_changed(tmp_path: Path) -> None:
    run_id, env = _launch(tmp_path, "normal")
    assert _await(tmp_path, env, run_id)["status"] == AWAITING
    run_dir = tmp_path / ".harness" / "runs" / run_id
    (run_dir / "work" / "src" / "app.txt").write_text("tampered\n", encoding="utf-8")
    path = _write_verdict(tmp_path, run_id)
    rejected = _cli(tmp_path, env, "final-review", "--run", run_id, "--file", str(path))
    assert rejected.returncode == 2
    assert "テスト時から変更" in rejected.stderr
    assert (tmp_path / "src" / "app.txt").read_text(encoding="utf-8") == "old\n"


def test_final_review_failure_ends_the_run_without_applying(tmp_path: Path) -> None:
    run_id, env = _launch(tmp_path, "normal")
    assert _await(tmp_path, env, run_id)["status"] == AWAITING
    path = _write_verdict(tmp_path, run_id, status="FAIL", findings=[_finding(tmp_path, run_id)])
    refused = _cli(tmp_path, env, "final-review", "--run", run_id, "--file", str(path))
    assert refused.returncode == 1
    record = _status(tmp_path, env, run_id)
    assert record["status"] == "FAILED"
    assert (tmp_path / "src" / "app.txt").read_text(encoding="utf-8") == "old\n"


def test_resume_is_refused_while_final_review_is_pending(tmp_path: Path) -> None:
    run_id, env = _launch(tmp_path, "normal")
    assert _await(tmp_path, env, run_id)["status"] == AWAITING
    refused = _cli(tmp_path, env, "resume", "--run", run_id)
    assert refused.returncode == 2
    assert "final-review" in refused.stderr
    assert _status(tmp_path, env, run_id)["status"] == AWAITING


def test_final_review_waiting_time_is_not_charged_to_the_run_budget(tmp_path: Path) -> None:
    run_id, env = _launch(tmp_path, "normal")
    assert _await(tmp_path, env, run_id)["status"] == AWAITING
    assert _excluded_seconds(tmp_path, run_id) == 0.0
    time.sleep(2.0)
    assert _accept(tmp_path, env, run_id)["status"] == "COMPLETED"
    assert _excluded_seconds(tmp_path, run_id) >= 2.0


def test_apply_conflict_refuses_all_harness_changes(tmp_path: Path) -> None:
    run_id, env = _launch(tmp_path, "apply-conflict")
    assert _await(tmp_path, env, run_id)["status"] == AWAITING
    record = _accept(tmp_path, env, run_id)
    assert record["status"] == "FAILED", record
    assert (tmp_path / "src" / "app.txt").read_text(encoding="utf-8") == "user change\n"


def test_stop_before_apply_can_resume_safely(tmp_path: Path) -> None:
    run_id, env = _launch(tmp_path, "normal")
    assert _await(tmp_path, env, run_id)["status"] == AWAITING
    path = _write_verdict(tmp_path, run_id)
    (tmp_path / ".harness" / "runs" / run_id / "STOP").touch()
    assert _cli(tmp_path, env, "final-review", "--run", run_id, "--file", str(path)).returncode == 0
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        record = _status(tmp_path, env, run_id)
        if record["status"] in TERMINAL:
            break
        time.sleep(0.2)
    assert record["status"] == "STOPPED", record
    assert (tmp_path / "src" / "app.txt").read_text(encoding="utf-8") == "old\n"
    assert _cli(tmp_path, env, "resume", "--run", run_id).returncode == 0
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        record = _status(tmp_path, env, run_id)
        if record["status"] in TERMINAL:
            break
        time.sleep(0.2)
    assert record["status"] == "COMPLETED", record
    assert (tmp_path / "src" / "app.txt").read_text(encoding="utf-8") == "ok\n"
