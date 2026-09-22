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


def _task(
    project: Path, *, max_fixes: int = 2, requirement_files: list[str] | None = None
) -> Path:
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
        "timeouts": {
            "implementation": 10,
            "testing": 10,
            "review": 10,
            "fix": 10,
            "total": 40,
        },
        "max_fixes": max_fixes,
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
        timeout=10,
        startupinfo=startupinfo,
        creationflags=creationflags,
    )


def _write_verdict(
    project: Path,
    run_id: str,
    *,
    status: str = "PASS",
    findings: list[dict] | None = None,
    model: str = "claude-fable-5-1",
    name: str = "verdict.json",
) -> Path:
    run_dir = project / ".harness" / "runs" / run_id
    path = run_dir / name
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
    run_dir = project / ".harness" / "runs" / run_id
    return {
        "id": "F1",
        "severity": severity,
        "description": "最終レビューの必須指摘",
        "evidence": str(run_dir / "final-review-request.json"),
    }


def _settle(
    project: Path,
    env: dict[str, str],
    run_id: str,
    *,
    timeout: float = 45,
    verdict: str = "PASS",
    findings: list[dict] | None = None,
) -> dict:
    """最終レビュー待ちになったら判定を提出しつつ、終端状態まで進める。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _cli(project, env, "status", "--run", run_id, "--json")
        assert status.returncode == 0, status.stderr
        record = json.loads(status.stdout)
        if record["status"] == AWAITING:
            path = _write_verdict(project, run_id, status=verdict, findings=findings)
            submitted = _cli(project, env, "final-review", "--run", run_id, "--file", str(path))
            assert submitted.returncode == 0, submitted.stderr
            verdict, findings = "PASS", None
            continue
        if record["status"] in TERMINAL:
            return record
        time.sleep(0.2)
    pytest.fail(f"run {run_id} did not terminate within {timeout} seconds")


def _run_to_terminal(
    project: Path, scenario: str, *, max_fixes: int = 2
) -> tuple[str, dict, dict[str, str]]:
    env = _env(project, scenario)
    launched = _cli(project, env, "run", "--task", str(_task(project, max_fixes=max_fixes)))
    assert launched.returncode == 0, launched.stderr
    run_id = launched.stdout.strip()
    return run_id, _settle(project, env, run_id), env


def test_normal_run_uses_fixed_models_and_applies_only_after_test_and_review(
    tmp_path: Path,
) -> None:
    run_id, status, _ = _run_to_terminal(tmp_path, "normal")
    assert status["status"] == "COMPLETED", status
    assert (tmp_path / "src" / "app.txt").read_text(encoding="utf-8") == "ok\n"

    invocations = sorted((tmp_path / ".harness" / "runs" / run_id / "phases").glob("*/invocation.json"))
    calls = [json.loads(path.read_text(encoding="utf-8")) for path in invocations]
    assert [(call["model"], call["reasoning_effort"]) for call in calls] == [
        ("gpt-5.6-sol", "medium"),
        ("gpt-5.6-sol", "medium"),
        ("gpt-6-astra", "high"),
    ]
    assert len({path.parent for path in invocations}) == 3


def test_forged_verification_record_cannot_complete(tmp_path: Path) -> None:
    _, status, _ = _run_to_terminal(tmp_path, "forge-verification")
    assert status["status"] != "COMPLETED", status
    assert (tmp_path / "src" / "app.txt").read_text(encoding="utf-8") == "old\n"


@pytest.mark.parametrize("scenario", ["test-fail", "review-fail"])
def test_failure_is_fixed_then_retested_and_reviewed(tmp_path: Path, scenario: str) -> None:
    run_id, status, _ = _run_to_terminal(tmp_path, scenario)
    assert status["status"] == "COMPLETED", status
    assert status["fix_count"] == 1
    phases = list((tmp_path / ".harness" / "runs" / run_id / "phases").glob("*"))
    assert len(phases) >= 5
    if scenario == "test-fail":
        fix_prompt = next(
            (tmp_path / ".harness" / "runs" / run_id / "phases").glob("03-*/prompt.txt")
        ).read_text(encoding="utf-8")
        assert "exit_code" in fix_prompt
        assert "AssertionError" in fix_prompt



@pytest.mark.parametrize("scenario", ["schema-invalid", "scope-violation"])
def test_invalid_agent_output_or_scope_violation_fails_closed(
    tmp_path: Path, scenario: str
) -> None:
    _, status, _ = _run_to_terminal(tmp_path, scenario, max_fixes=0)
    assert status["status"] == "FAILED", status
    assert (tmp_path / "src" / "app.txt").read_text(encoding="utf-8") == "old\n"


def test_fix_limit_failure_cannot_be_resumed_for_free_attempt(tmp_path: Path) -> None:
    run_id, status, env = _run_to_terminal(tmp_path, "schema-invalid", max_fixes=0)
    assert status["status"] == "FAILED"
    resumed = _cli(tmp_path, env, "resume", "--run", run_id)
    assert resumed.returncode == 2
    after = json.loads(_cli(tmp_path, env, "status", "--run", run_id, "--json").stdout)
    assert after["status"] == "FAILED"
    assert after["fix_count"] == 0


def test_apply_conflict_refuses_all_harness_changes(tmp_path: Path) -> None:
    _, status, _ = _run_to_terminal(tmp_path, "apply-conflict")
    assert status["status"] == "FAILED", status
    assert (tmp_path / "src" / "app.txt").read_text(encoding="utf-8") == "user change\n"


def test_stop_then_resume_preserves_run_and_completes(tmp_path: Path) -> None:
    env = _env(tmp_path, "stop-resume")
    launched = _cli(tmp_path, env, "run", "--task", str(_task(tmp_path)))
    assert launched.returncode == 0, launched.stderr
    run_id = launched.stdout.strip()
    run_dir = tmp_path / ".harness" / "runs" / run_id
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not list((run_dir / "phases").glob("01-*/result.json")):
        time.sleep(0.1)
    stopped = _cli(tmp_path, env, "stop", "--run", run_id)
    assert stopped.returncode == 0, stopped.stderr
    while time.monotonic() < deadline:
        status = json.loads(_cli(tmp_path, env, "status", "--run", run_id, "--json").stdout)
        if status["status"] == "STOPPED":
            break
        time.sleep(0.1)
    assert status["status"] == "STOPPED", status
    created = status["created"]
    resumed = _cli(tmp_path, env, "resume", "--run", run_id)
    assert resumed.returncode == 0, resumed.stderr
    status = _settle(tmp_path, env, run_id, timeout=30)
    assert status["status"] == "COMPLETED", status
    assert status["created"] == created
    assert status["fix_count"] == 0


def test_dead_controller_is_reconciled_and_run_can_resume(tmp_path: Path) -> None:
    env = _env(tmp_path, "stop-resume")
    launched = _cli(tmp_path, env, "run", "--task", str(_task(tmp_path)))
    assert launched.returncode == 0, launched.stderr
    run_id = launched.stdout.strip()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = json.loads(_cli(tmp_path, env, "status", "--run", run_id, "--json").stdout)
        if status["status"] == "RUNNING" and status["controller_pid"]:
            break
        time.sleep(0.1)
    assert status["status"] == "RUNNING", status
    first_invocation = next(
        (tmp_path / ".harness" / "runs" / run_id / "phases").glob("01-*/invocation.json")
    )
    first_timeout = json.loads(first_invocation.read_text(encoding="utf-8"))["timeout"]
    time.sleep(1.3)
    os.kill(int(status["controller_pid"]), signal.SIGTERM)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = json.loads(_cli(tmp_path, env, "status", "--run", run_id, "--json").stdout)
        if status["status"] != "RUNNING":
            break
        time.sleep(0.2)
    assert status["status"] in {"FAILED", "STOPPED", "BLOCKED"}, status
    resumed = _cli(tmp_path, env, "resume", "--run", run_id)
    assert resumed.returncode == 0, resumed.stderr
    phases = tmp_path / ".harness" / "runs" / run_id / "phases"
    deadline = time.monotonic() + 10
    second = None
    while time.monotonic() < deadline:
        matches = list(phases.glob("02-*/invocation.json"))
        if matches:
            second = matches[0]
            break
        time.sleep(0.1)
    assert second is not None
    second_timeout = json.loads(second.read_text(encoding="utf-8"))["timeout"]
    assert second_timeout < first_timeout
    _cli(tmp_path, env, "stop", "--run", run_id)


def test_repeated_resume_deducts_same_phase_budget_and_preserves_review_context(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path, "review-stop-twice")
    launched = _cli(tmp_path, env, "run", "--task", str(_task(tmp_path)))
    assert launched.returncode == 0, launched.stderr
    run_id = launched.stdout.strip()
    phases = tmp_path / ".harness" / "runs" / run_id / "phases"

    for sequence in (4, 5):
        deadline = time.monotonic() + 15
        prompt = None
        while time.monotonic() < deadline:
            matches = list(phases.glob(f"{sequence:02d}-*/prompt.txt"))
            if matches:
                prompt = matches[0]
                break
            time.sleep(0.1)
        assert prompt is not None, f"phase {sequence} did not start"
        time.sleep(0.7)
        assert _cli(tmp_path, env, "stop", "--run", run_id).returncode == 0
        while time.monotonic() < deadline:
            status = json.loads(_cli(tmp_path, env, "status", "--run", run_id, "--json").stdout)
            if status["status"] == "STOPPED":
                break
            time.sleep(0.1)
        assert status["status"] == "STOPPED", status
        assert _cli(tmp_path, env, "resume", "--run", run_id).returncode == 0

    status = _settle(tmp_path, env, run_id, timeout=30)
    assert status["status"] == "COMPLETED", status

    fix_invocations = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(phases.glob("0[456]-*/invocation.json"))
    ]
    assert len(fix_invocations) == 3
    assert fix_invocations[0]["timeout"] > fix_invocations[1]["timeout"] > fix_invocations[2]["timeout"]
    for prompt_path in phases.glob("0[456]-*/prompt.txt"):
        assert "must preserve this finding" in prompt_path.read_text(encoding="utf-8")


def test_stopped_implementation_can_resume_when_max_fixes_is_zero(tmp_path: Path) -> None:
    env = _env(tmp_path, "stop-resume")
    launched = _cli(tmp_path, env, "run", "--task", str(_task(tmp_path, max_fixes=0)))
    assert launched.returncode == 0, launched.stderr
    run_id = launched.stdout.strip()
    run_dir = tmp_path / ".harness" / "runs" / run_id
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not list((run_dir / "phases").glob("01-*/result.json")):
        time.sleep(0.1)
    assert _cli(tmp_path, env, "stop", "--run", run_id).returncode == 0
    while time.monotonic() < deadline:
        status = json.loads(_cli(tmp_path, env, "status", "--run", run_id, "--json").stdout)
        if status["status"] == "STOPPED":
            break
        time.sleep(0.1)
    assert status["status"] == "STOPPED", status
    assert _cli(tmp_path, env, "resume", "--run", run_id).returncode == 0
    _cli(tmp_path, env, "stop", "--run", run_id)


def test_second_fix_stopped_mid_phase_can_resume(tmp_path: Path) -> None:
    env = _env(tmp_path, "second-fix-stop")
    launched = _cli(tmp_path, env, "run", "--task", str(_task(tmp_path, max_fixes=2)))
    assert launched.returncode == 0, launched.stderr
    run_id = launched.stdout.strip()
    phases = tmp_path / ".harness" / "runs" / run_id / "phases"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not list(phases.glob("05-*/prompt.txt")):
        time.sleep(0.1)
    assert list(phases.glob("05-*/prompt.txt")), "second FIX did not start"
    assert _cli(tmp_path, env, "stop", "--run", run_id).returncode == 0
    while time.monotonic() < deadline:
        status = json.loads(_cli(tmp_path, env, "status", "--run", run_id, "--json").stdout)
        if status["status"] == "STOPPED":
            break
        time.sleep(0.1)
    assert status["status"] == "STOPPED", status
    assert status["fix_count"] == 2
    assert _cli(tmp_path, env, "resume", "--run", run_id).returncode == 0
    status = _settle(tmp_path, env, run_id, timeout=30)
    assert status["status"] == "COMPLETED", status


def test_missing_requirement_file_does_not_leave_permanent_active_run(tmp_path: Path) -> None:
    env = _env(tmp_path, "normal")
    launched = _cli(
        tmp_path,
        env,
        "run",
        "--task",
        str(_task(tmp_path, requirement_files=["missing.md"])),
    )
    if launched.returncode != 0:
        assert launched.returncode == 2
        return
    run_id = launched.stdout.strip()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = json.loads(_cli(tmp_path, env, "status", "--run", run_id, "--json").stdout)
        if status["status"] not in {"QUEUED", "RUNNING"}:
            break
        time.sleep(0.2)
    assert status["status"] not in {"QUEUED", "RUNNING"}, status


def test_requirement_file_is_frozen_and_agent_change_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "requirements.md").write_text("original requirement", encoding="utf-8")
    env = _env(tmp_path, "mutate-requirement")
    launched = _cli(
        tmp_path,
        env,
        "run",
        "--task",
        str(_task(tmp_path, requirement_files=["requirements.md"])),
    )
    assert launched.returncode == 0, launched.stderr
    run_id = launched.stdout.strip()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = json.loads(_cli(tmp_path, env, "status", "--run", run_id, "--json").stdout)
        if status["status"] in TERMINAL:
            break
        time.sleep(0.2)
    assert status["status"] == "FAILED", status
    assert (tmp_path / "requirements.md").read_text(encoding="utf-8") == "original requirement"
    prompt = next((tmp_path / ".harness" / "runs" / run_id / "phases").glob("01-*/prompt.txt"))
    assert "original requirement" in prompt.read_text(encoding="utf-8")


def test_stop_after_review_before_apply_can_resume_safely(tmp_path: Path) -> None:
    run_id, status, env = _run_to_terminal(tmp_path, "pre-apply-stop")
    assert status["status"] == "STOPPED", status
    assert (tmp_path / "src" / "app.txt").read_text(encoding="utf-8") == "old\n"
    resumed = _cli(tmp_path, env, "resume", "--run", run_id)
    assert resumed.returncode == 0, resumed.stderr
    status = _settle(tmp_path, env, run_id, timeout=20)
    assert status["status"] == "COMPLETED", status
    assert (tmp_path / "src" / "app.txt").read_text(encoding="utf-8") == "ok\n"


def _launch(project: Path, scenario: str, *, max_fixes: int = 2) -> tuple[str, dict[str, str]]:
    env = _env(project, scenario)
    launched = _cli(project, env, "run", "--task", str(_task(project, max_fixes=max_fixes)))
    assert launched.returncode == 0, launched.stderr
    return launched.stdout.strip(), env


def _await_final_review(
    project: Path, env: dict[str, str], run_id: str, *, timeout: float = 30
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = json.loads(_cli(project, env, "status", "--run", run_id, "--json").stdout)
        if record["status"] == AWAITING:
            return record
        assert record["status"] not in TERMINAL, record
        time.sleep(0.2)
    pytest.fail(f"run {run_id} did not reach final review within {timeout} seconds")


def _excluded_seconds(project: Path, run_id: str) -> float:
    with sqlite3.connect(project / ".harness" / "state.sqlite3") as db:
        row = db.execute("SELECT excluded_seconds FROM runs WHERE id=?", (run_id,)).fetchone()
    return float(row[0])


def test_astra_pass_stops_for_claude_final_review_before_applying(tmp_path: Path) -> None:
    run_id, env = _launch(tmp_path, "normal")
    record = _await_final_review(tmp_path, env, run_id)
    assert record["phase"] == "最終レビュー"
    assert record["controller_pid"] is None
    assert (tmp_path / "src" / "app.txt").read_text(encoding="utf-8") == "old\n"

    run_dir = tmp_path / ".harness" / "runs" / run_id
    request = json.loads((run_dir / "final-review-request.json").read_text(encoding="utf-8"))
    assert request["changed_files"] == ["src/app.txt"]
    assert request["tested_code_hash"] == (run_dir / "tested-code-hash.txt").read_text(
        encoding="ascii"
    )
    assert request["work_root"] == str(run_dir / "work")
    assert len(request["phases"]) == 3


def test_wait_returns_when_claude_has_to_decide(tmp_path: Path) -> None:
    run_id, env = _launch(tmp_path, "normal")
    waited = _cli(tmp_path, env, "wait", "--run", run_id, "--json", "--timeout", "8")
    assert waited.returncode == 0, waited.stderr
    assert json.loads(waited.stdout)["status"] == AWAITING


def test_final_review_pass_applies_changes_and_records_verdict(tmp_path: Path) -> None:
    run_id, env = _launch(tmp_path, "normal")
    _await_final_review(tmp_path, env, run_id)
    path = _write_verdict(tmp_path, run_id)
    assert _cli(tmp_path, env, "final-review", "--run", run_id, "--file", str(path)).returncode == 0
    status = _settle(tmp_path, env, run_id, timeout=20)
    assert status["status"] == "COMPLETED", status
    assert (tmp_path / "src" / "app.txt").read_text(encoding="utf-8") == "ok\n"

    run_dir = tmp_path / ".harness" / "runs" / run_id
    assert json.loads((run_dir / "changes.json").read_text(encoding="utf-8")) == ["src/app.txt"]
    verdict = json.loads((run_dir / "final-review.json").read_text(encoding="utf-8"))
    assert verdict["reviewer"]["model"] == "claude-fable-5-1"
    decisions = (run_dir / "control-decisions.jsonl").read_text(encoding="utf-8").splitlines()
    assert any(json.loads(line)["phase"] == "最終レビュー" for line in decisions)


@pytest.mark.parametrize("mutate", ["schema", "model", "evidence", "pass-with-blocking"])
def test_final_review_rejects_untrustworthy_verdict(tmp_path: Path, mutate: str) -> None:
    run_id, env = _launch(tmp_path, "normal")
    _await_final_review(tmp_path, env, run_id)
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
    after = json.loads(_cli(tmp_path, env, "status", "--run", run_id, "--json").stdout)
    assert after["status"] == AWAITING
    assert not (run_dir / "final-review.json").exists()
    assert (tmp_path / "src" / "app.txt").read_text(encoding="utf-8") == "old\n"


def test_final_review_rejects_verdict_when_work_copy_changed(tmp_path: Path) -> None:
    run_id, env = _launch(tmp_path, "normal")
    _await_final_review(tmp_path, env, run_id)
    run_dir = tmp_path / ".harness" / "runs" / run_id
    (run_dir / "work" / "src" / "app.txt").write_text("tampered\n", encoding="utf-8")
    path = _write_verdict(tmp_path, run_id)
    rejected = _cli(tmp_path, env, "final-review", "--run", run_id, "--file", str(path))
    assert rejected.returncode == 2
    assert "テスト時から変更" in rejected.stderr
    assert (tmp_path / "src" / "app.txt").read_text(encoding="utf-8") == "old\n"


def test_final_review_findings_send_run_back_to_fix_and_retest(tmp_path: Path) -> None:
    run_id, env = _launch(tmp_path, "final-review-fix")
    _await_final_review(tmp_path, env, run_id)
    path = _write_verdict(tmp_path, run_id, status="FAIL", findings=[_finding(tmp_path, run_id)])
    assert _cli(tmp_path, env, "final-review", "--run", run_id, "--file", str(path)).returncode == 0
    second = _await_final_review(tmp_path, env, run_id, timeout=40)
    assert second["fix_count"] == 1
    phases = tmp_path / ".harness" / "runs" / run_id / "phases"
    assert sorted(p.name.split("-", 1)[1] for p in phases.glob("0[456]-*")) == [
        "テスト",
        "レビュー",
        "修正",
    ]
    fix_prompt = next(phases.glob("04-*/prompt.txt")).read_text(encoding="utf-8")
    assert "最終レビューの必須指摘" in fix_prompt
    status = _settle(tmp_path, env, run_id, timeout=25)
    assert status["status"] == "COMPLETED", status


def test_final_review_failure_at_fix_limit_stops_the_run(tmp_path: Path) -> None:
    run_id, env = _launch(tmp_path, "normal", max_fixes=0)
    _await_final_review(tmp_path, env, run_id)
    path = _write_verdict(tmp_path, run_id, status="FAIL", findings=[_finding(tmp_path, run_id)])
    refused = _cli(tmp_path, env, "final-review", "--run", run_id, "--file", str(path))
    assert refused.returncode == 1
    record = json.loads(_cli(tmp_path, env, "status", "--run", run_id, "--json").stdout)
    assert record["status"] == "FAILED"
    assert (tmp_path / "src" / "app.txt").read_text(encoding="utf-8") == "old\n"


def test_resume_is_refused_while_final_review_is_pending(tmp_path: Path) -> None:
    run_id, env = _launch(tmp_path, "normal")
    _await_final_review(tmp_path, env, run_id)
    refused = _cli(tmp_path, env, "resume", "--run", run_id)
    assert refused.returncode == 2
    assert "final-review" in refused.stderr
    after = json.loads(_cli(tmp_path, env, "status", "--run", run_id, "--json").stdout)
    assert after["status"] == AWAITING


def test_final_review_waiting_time_is_not_charged_to_the_run_budget(tmp_path: Path) -> None:
    run_id, env = _launch(tmp_path, "normal")
    _await_final_review(tmp_path, env, run_id)
    assert _excluded_seconds(tmp_path, run_id) == 0.0
    time.sleep(2.0)
    path = _write_verdict(tmp_path, run_id)
    assert _cli(tmp_path, env, "final-review", "--run", run_id, "--file", str(path)).returncode == 0
    assert _excluded_seconds(tmp_path, run_id) >= 2.0
    assert _settle(tmp_path, env, run_id, timeout=20)["status"] == "COMPLETED"
