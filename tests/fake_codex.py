"""Scripted codex executable used only by the black-box harness tests."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def _write_result(path: Path, *, status: str = "PASS", issues: list[str] | None = None) -> None:
    path.write_text(
        json.dumps(
            {
                "status": status,
                "summary": "scripted contractor",
                "issues": issues if issues is not None else ([] if status == "PASS" else ["x"]),
            }
        ),
        encoding="utf-8",
    )


def main() -> int:
    args = sys.argv[1:]
    result_path = Path(args[args.index("--output-last-message") + 1])
    sequence = int(result_path.parent.name)
    run_dir = result_path.parents[2]
    work = Path.cwd()
    scenario = os.environ.get("DEV_HARNESS_TEST_SCENARIO", "normal")

    if scenario == "schema-invalid":
        result_path.write_text('{"status":"PASS"}', encoding="utf-8")
        return 0
    if scenario == "blocked":
        _write_result(result_path, status="BLOCKED", issues=["要件が不足している"])
        return 0
    if scenario == "self-report-fail":
        _write_result(result_path, status="FAIL", issues=["納品できなかった"])
        return 0
    if scenario == "mutate-requirement":
        (work / "requirements.md").write_text("changed", encoding="utf-8")
        _write_result(result_path)
        return 0
    if scenario == "scope-violation":
        (work / "outside.txt").write_text("forbidden", encoding="utf-8")
        _write_result(result_path)
        return 0
    if scenario == "apply-conflict":
        # 発注元の原本を第三者が触った状態を作る。
        (result_path.parents[5] / "src" / "app.txt").write_text("user change\n", encoding="utf-8")

    target = work / "src" / "app.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("bad\n" if scenario == "verification-fail" else "ok\n", encoding="utf-8")
    (work / "src" / "test_app.py").write_text(
        "def test_placeholder():\n    assert True\n", encoding="utf-8"
    )
    _write_result(result_path)

    if scenario == "slow" and sequence == 1:
        time.sleep(60)
    if scenario == "pre-apply-stop":
        (run_dir / "STOP").touch()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
