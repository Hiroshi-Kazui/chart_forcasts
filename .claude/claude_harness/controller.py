from __future__ import annotations

import fnmatch
import json
import os
import sys
import time
import traceback
from pathlib import Path

from codex_harness.invocation import schema_path
from codex_harness.prompts import build_prompt

from .agent_runner import invoke
from .config import Task
from .final_review import AWAITING, blocking_findings, check_verdict
from .liveness import close_marker, create_marker
from .store import Store
from .verify import code_hash, execute
from .workspace import apply_changes, make_copy, snapshot_hashes, write_baseline

ORDER, VERIFY, REVIEW, APPLY = "発注", "検証", "最終レビュー", "反映"


def _valid_result(value: dict | None) -> bool:
    return bool(
        value
        and value.get("status") in {"PASS", "FAIL", "BLOCKED"}
        and isinstance(value.get("summary"), str)
        and isinstance(value.get("issues"), list)
        and all(isinstance(x, str) for x in value.get("issues", []))
    )


def _integrity(runtime_parent: Path, task_path: Path, requirements_path: Path) -> dict:
    return {
        "runtime": snapshot_hashes(runtime_parent),
        "task": task_path.read_bytes().hex(),
        "requirements": requirements_path.read_bytes().hex(),
    }


def _outside(base: dict[str, str], after: dict[str, str], allowed: tuple[str, ...]) -> list[str]:
    changed = {p for p in set(base) | set(after) if base.get(p) != after.get(p)}
    return sorted(
        p
        for p in changed
        if not any(
            fnmatch.fnmatch(p, r) or p == r or p.startswith(r.rstrip("/") + "/") for r in allowed
        )
    )


def _append_decision(path: Path, value: dict) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"time": time.time(), **value}, ensure_ascii=False) + "\n")


def write_progress(path: Path, phase: str, spent: float, **extra) -> None:
    path.write_text(
        json.dumps(
            {"phase": phase, "spent": spent, "started_at": None, **extra}, ensure_ascii=False
        ),
        encoding="utf-8",
    )


def _final_review_request(
    run_id: str, task: Task, root: Path, work: Path, run_dir: Path, changed: list[str]
) -> dict:
    return {
        "run_id": run_id,
        "task_name": task.name,
        "requirements": list(task.requirements),
        "acceptance_criteria": list(task.acceptance_criteria),
        "edit_scope": list(task.edit_scope),
        "original_root": str(root),
        "work_root": str(work),
        "baseline": str(run_dir / "baseline.json"),
        "frozen_requirements": str(run_dir / "frozen-requirements.json"),
        "tested_code_hash": (run_dir / "tested-code-hash.txt").read_text(encoding="ascii"),
        "changed_files": changed,
        "deliveries": sorted(str(p) for p in (run_dir / "deliveries").glob("*")),
        "verification": str(run_dir / "verification.json"),
        "control_history": str(run_dir / "control-decisions.jsonl"),
        "verdict_schema": str(
            run_dir / "runtime" / "claude_harness" / "schemas" / "final-review.json"
        ),
        "submit_command": [
            "python",
            "-m",
            "claude_harness",
            "final-review",
            "--run",
            run_id,
            "--file",
            "<判定JSONのパス>",
        ],
    }


def _complete(
    store: Store,
    run_id: str,
    root: Path,
    work: Path,
    baseline: dict[str, str],
    task: Task,
    run_dir: Path,
) -> int:
    changes = apply_changes(root, work, baseline, task.edit_scope, task.exclude)
    (run_dir / "changes.json").write_text(json.dumps(changes, ensure_ascii=False), encoding="utf-8")
    (run_dir / "report.md").write_text(
        f"# 開発ハーネス実行結果\n\n状態: 完了\n\n発注: {task.name}\n\n"
        f"反映: {', '.join(changes) or 'なし'}\n",
        encoding="utf-8",
    )
    store.update(
        run_id, status="COMPLETED", phase="終了", message="検証と最終レビューに合格しました"
    )
    return 0


def run_controller(root: Path, run_id: str) -> int:
    marker = create_marker(run_id)
    store, run_dir = Store(root), root / ".harness" / "runs" / run_id
    row = store.get(run_id)
    stop_file, task_path = run_dir / "STOP", run_dir / "task.json"
    runtime_parent, work = run_dir / "runtime", run_dir / "work"
    baseline_path, progress_path = run_dir / "baseline.json", run_dir / "progress.json"
    integrity_path, task = run_dir / "integrity.json", Task.load(task_path)
    frozen_requirements_path = run_dir / "frozen-requirements.json"
    decisions_path = run_dir / "control-decisions.jsonl"
    verification_path = run_dir / "verification.json"
    created, excluded = float(row["created"]), float(row["excluded_seconds"])
    try:
        if stop_file.exists():
            store.update(
                run_id, status="STOPPED", phase=row["phase"], message="起動前に停止されました"
            )
            return 2
        if not frozen_requirements_path.exists():
            frozen_requirements_path.write_text(
                json.dumps(
                    {
                        name: (root / name).read_text(encoding="utf-8")
                        for name in task.requirement_files
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        frozen_requirements = json.loads(frozen_requirements_path.read_text(encoding="utf-8"))
        expected_integrity = _integrity(runtime_parent, task_path, frozen_requirements_path)
        if integrity_path.exists() and json.loads(integrity_path.read_text()) != expected_integrity:
            raise RuntimeError("固定した実行環境またはタスク定義が変更されました")
        integrity_path.write_text(json.dumps(expected_integrity), encoding="utf-8")
        store.update(run_id, status="RUNNING", controller_pid=os.getpid(), message="")
        if not work.exists():
            baseline = make_copy(root, work, task.exclude)
            write_baseline(baseline_path, baseline)
        else:
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

        if progress_path.exists():
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            phase, spent = progress["phase"], float(progress["spent"])
            if progress.get("started_at") is not None:
                spent += max(0.0, time.time() - float(progress["started_at"]))
        else:
            phase, spent = ORDER, 0.0

        def over_total() -> bool:
            return time.time() - created - excluded >= task.timeouts.total

        if phase == APPLY:
            verdict = json.loads((run_dir / "final-review.json").read_text(encoding="utf-8"))
            problems = check_verdict(verdict, run_dir, root, work, run_dir / "tested-code-hash.txt")
            if verdict.get("status") != "PASS" or blocking_findings(verdict):
                problems.append("最終レビューが合格していません")
            if problems:
                store.update(run_id, status="FAILED", phase=REVIEW, message="; ".join(problems))
                return 1
            if stop_file.exists():
                write_progress(progress_path, APPLY, 0.0)
                store.update(run_id, status="STOPPED", phase=APPLY, message="停止指示を受けました")
                return 2
            if over_total():
                store.update(
                    run_id, status="FAILED", phase="終了", message="全体時間上限を超過しました"
                )
                return 1
            if _integrity(runtime_parent, task_path, frozen_requirements_path) != expected_integrity:
                raise RuntimeError("固定した実行環境またはタスク定義が変更されました")
            _append_decision(decisions_path, {"phase": APPLY, "decision": "PASS"})
            return _complete(store, run_id, root, work, baseline, task, run_dir)

        if phase == ORDER:
            if over_total():
                store.update(
                    run_id, status="FAILED", phase=ORDER, message="全体時間上限を超過しました"
                )
                return 1
            if spent >= task.timeouts.delivery:
                store.update(
                    run_id, status="FAILED", phase=ORDER, message="発注の時間上限を超過しました"
                )
                return 1
            timeout = min(
                max(1, int(task.timeouts.delivery - spent)),
                max(1, int(task.timeouts.total - (time.time() - created - excluded))),
            )
            deliveries = run_dir / "deliveries"
            sequence = len(list(deliveries.glob("*"))) + 1 if deliveries.exists() else 1
            delivery_dir = deliveries / f"{sequence:02d}"
            write_progress(progress_path, ORDER, spent, started_at=time.time())
            store.update(run_id, phase=ORDER, message="受注側が作業中です")
            before = snapshot_hashes(work, task.exclude)
            proc, result = invoke(
                build_prompt(task, frozen_requirements),
                work,
                delivery_dir,
                schema_path(runtime_parent),
                timeout,
                stop_file,
            )
            after = snapshot_hashes(work, task.exclude)
            (delivery_dir / "process.json").write_text(json.dumps(vars(proc)), encoding="utf-8")
            changed_requirements = [
                name for name in task.requirement_files if baseline.get(name) != after.get(name)
            ]
            if changed_requirements:
                store.update(
                    run_id,
                    status="FAILED",
                    phase=ORDER,
                    message="固定した要件ファイルが変更されました: "
                    + ", ".join(changed_requirements),
                )
                return 1
            if proc.stopped:
                write_progress(progress_path, ORDER, spent + proc.duration)
                store.update(run_id, status="STOPPED", phase=ORDER, message="停止指示を受けました")
                return 2
            if proc.timed_out:
                store.update(
                    run_id, status="FAILED", phase=ORDER, message="発注の時間上限を超過しました"
                )
                return 1
            if proc.exit_code != 0 or not _valid_result(result):
                store.update(
                    run_id,
                    status="FAILED",
                    phase=ORDER,
                    message="受注側の実行または結果JSONが不正です",
                )
                return 1
            assert result is not None
            if result["status"] == "BLOCKED":
                write_progress(progress_path, ORDER, spent + proc.duration)
                store.update(
                    run_id, status="BLOCKED", phase=ORDER, message="; ".join(result["issues"])
                )
                return 3
            outside = _outside(baseline, after, task.edit_scope)
            if outside:
                store.update(
                    run_id,
                    status="FAILED",
                    phase=ORDER,
                    message="編集範囲外の変更: " + ", ".join(outside),
                )
                return 1
            _append_decision(
                decisions_path,
                {"phase": ORDER, "decision": result["status"], "delivery": str(delivery_dir)},
            )
            if result["status"] != "PASS":
                store.update(
                    run_id,
                    status="FAILED",
                    phase=ORDER,
                    message="受注側が未完了を申告: " + "; ".join(result["issues"]),
                )
                return 1
            phase = VERIFY
            write_progress(progress_path, VERIFY, 0.0)

        if phase == VERIFY:
            if stop_file.exists():
                store.update(run_id, status="STOPPED", phase=VERIFY, message="停止指示を受けました")
                return 2
            if over_total():
                store.update(
                    run_id, status="FAILED", phase=VERIFY, message="全体時間上限を超過しました"
                )
                return 1
            store.update(run_id, phase=VERIFY, message="固定検証を実行中です")
            record = execute(task, work, verification_path, task.timeouts.verification, stop_file)
            failed = [
                {
                    "argv": item["argv"],
                    "exit_code": item["exit_code"],
                    "timed_out": item["timed_out"],
                    "stopped": item["stopped"],
                    "stderr": str(item.get("stderr", ""))[-2000:],
                }
                for item in record["commands"]
                if item["exit_code"] != 0 or item["timed_out"] or item["stopped"]
            ]
            _append_decision(
                decisions_path,
                {
                    "phase": VERIFY,
                    "decision": "PASS" if record["success"] else "FAIL",
                    "evidence": str(verification_path),
                    "failed_commands": failed,
                },
            )
            if not record["success"] or record["code_hash"] != record["end_code_hash"]:
                store.update(
                    run_id,
                    status="FAILED",
                    phase=VERIFY,
                    message="固定検証が不合格です: " + str(verification_path),
                )
                return 1
            (run_dir / "tested-code-hash.txt").write_text(code_hash(work), encoding="ascii")
            after = snapshot_hashes(work, task.exclude)
            changed = sorted(
                p for p in set(baseline) | set(after) if baseline.get(p) != after.get(p)
            )
            (run_dir / "final-review-request.json").write_text(
                json.dumps(
                    _final_review_request(run_id, task, root, work, run_dir, changed),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            write_progress(progress_path, REVIEW, 0.0, awaiting_since=time.time())
            store.update(
                run_id, status=AWAITING, phase=REVIEW, message="Claudeの最終レビュー待ちです"
            )
            return 4

        store.update(run_id, status="FAILED", phase=phase, message=f"再開できない工程です: {phase}")
        return 1
    except Exception as exc:
        (run_dir / "controller-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        store.update(run_id, status="FAILED", phase=locals().get("phase", "準備"), message=str(exc))
        return 1
    finally:
        try:
            store.update(run_id, controller_pid=None)
        except Exception:
            pass
        close_marker(marker)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("controllerにはリポジトリとrun_idが必要です", file=sys.stderr)
        return 2
    return run_controller(Path(argv[0]).resolve(), argv[1])


if __name__ == "__main__":
    raise SystemExit(main())
