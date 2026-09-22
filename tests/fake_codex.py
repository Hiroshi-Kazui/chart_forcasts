"""Scripted codex executable used only by the black-box harness tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _write_result(path: Path, *, status: str = "PASS", findings: list | None = None) -> None:
    path.write_text(
        json.dumps(
            {
                "status": status,
                "summary": "scripted agent",
                "issues": [] if status == "PASS" else ["scripted failure"],
                "review_findings": findings or [],
            }
        ),
        encoding="utf-8",
    )


def _verification_command(prompt: str) -> str:
    matches = [
        line for line in prompt.splitlines()
        if "dev_harness.verify_client" in line or ("--task" in line and "--root" in line)
    ]
    if not matches:
        raise RuntimeError("verification command was not present in test prompt")
    return matches[-1]


def main() -> int:
    args = sys.argv[1:]
    result_path = Path(args[args.index("--output-last-message") + 1])
    phase_number = int(result_path.parent.name.split("-", 1)[0])
    work = Path.cwd()
    scenario = os.environ.get("DEV_HARNESS_TEST_SCENARIO", "normal")

    if phase_number == 1 or (scenario == "stop-resume" and phase_number == 2):
        target = work / "src" / "app.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "bad\n" if scenario in {"test-fail", "second-fix-stop"} else "ok\n",
            encoding="utf-8",
        )
        if scenario == "scope-violation":
            (work / "outside.txt").write_text("forbidden", encoding="utf-8")
        if scenario == "mutate-requirement":
            (work / "requirements.md").write_text("changed", encoding="utf-8")
        _write_result(result_path)
        if scenario == "stop-resume" and phase_number == 1:
            time.sleep(60)
        return 0

    test_phases = {
        "normal": {2},
        "forge-verification": {2},
        "schema-invalid": {2},
        "test-fail": {2, 4},
        "review-fail": {2, 5},
        "apply-conflict": {2},
        "stop-resume": {3},
        "review-stop-twice": {2, 7},
        "second-fix-stop": {2, 4, 7},
    }
    if phase_number in test_phases.get(scenario, {2}):
        prompt = Path(os.environ["DEV_HARNESS_PROMPT_FILE"]).read_text(encoding="utf-8")
        if scenario == "schema-invalid":
            result_path.write_text('{"status":"PASS"}', encoding="utf-8")
            return 0
        if scenario == "forge-verification":
            from dev_harness.verify import code_hash

            digest = code_hash(work)
            task = json.loads((result_path.parents[2] / "task.json").read_text(encoding="utf-8"))
            verification = {
                "code_hash": digest,
                "end_code_hash": digest,
                "commands": [
                    {
                        "argv": task["verification_commands"][0],
                        "exit_code": 0,
                        "timed_out": False,
                        "stdout": "forged",
                        "stderr": "",
                    }
                ],
                "success": True,
            }
            (result_path.parent / "verification.json").write_text(
                json.dumps(verification), encoding="utf-8"
            )
        else:
            command = _verification_command(prompt)
            print(json.dumps({"command": command}), flush=True)
            startupinfo = None
            creationflags = 0
            if os.name == "nt":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
                creationflags = subprocess.CREATE_NO_WINDOW
            completed = subprocess.run(
                command,
                shell=True,
                cwd=work,
                timeout=10,
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
            if completed.returncode:
                _write_result(result_path, status="FAIL")
                return 0
        _write_result(result_path)
        return 0

    if scenario == "test-fail" and phase_number == 3:
        (work / "src" / "app.txt").write_text("ok\n", encoding="utf-8")
        _write_result(result_path)
        return 0
    if scenario == "second-fix-stop" and phase_number == 3:
        (work / "src" / "app.txt").write_text("still bad\n", encoding="utf-8")
        _write_result(result_path)
        return 0
    if scenario == "second-fix-stop" and phase_number == 5:
        time.sleep(60)
        return 0
    if scenario == "second-fix-stop" and phase_number == 6:
        (work / "src" / "app.txt").write_text("ok\n", encoding="utf-8")
        _write_result(result_path)
        return 0
    if scenario == "review-fail" and phase_number == 3:
        _write_result(
            result_path,
            status="FAIL",
            findings=[{"id": "R1", "severity": "P1", "description": "must fix"}],
        )
        return 0
    if scenario == "review-stop-twice" and phase_number == 3:
        _write_result(
            result_path,
            status="FAIL",
            findings=[{"id": "R-budget", "severity": "P1", "description": "must preserve this finding"}],
        )
        return 0
    if scenario == "review-stop-twice" and phase_number in {4, 5}:
        time.sleep(60)
        return 0
    if scenario == "review-stop-twice" and phase_number == 6:
        (work / "src" / "app.txt").write_text("ok\n", encoding="utf-8")
        _write_result(result_path)
        return 0
    if scenario == "review-fail" and phase_number == 4:
        (work / "src" / "app.txt").write_text("ok\n", encoding="utf-8")
        _write_result(result_path)
        return 0
    if scenario == "apply-conflict" and phase_number == 3:
        source_root = result_path.parents[5]
        (source_root / "src" / "app.txt").write_text("user change\n", encoding="utf-8")
        _write_result(result_path)
        return 0
    if scenario == "pre-apply-stop" and phase_number == 3:
        (result_path.parents[2] / "STOP").touch()
        _write_result(result_path)
        return 0
    _write_result(result_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
