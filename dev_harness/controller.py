from __future__ import annotations

import fnmatch
import json
import os
import sys
import time
import traceback
from pathlib import Path

from .agent import frozen_verify_command, invoke
from .config import Task
from .liveness import close_marker, create_marker
from .store import Store
from .verify import code_hash
from .verification_broker import VerificationBroker
from .workspace import apply_changes, make_copy, snapshot_hashes, write_baseline

PHASES = {"実装": "implementation", "テスト": "testing", "レビュー": "review", "修正": "fix"}


def _task_text(task: Task) -> str:
    return json.dumps(
        {
            "name": task.name,
            "requirements": task.requirements,
            "requirement_files": task.requirement_files,
            "edit_scope": task.edit_scope,
            "acceptance_criteria": task.acceptance_criteria,
            "verification_commands": task.verification_commands,
        },
        ensure_ascii=False,
        indent=2,
    )


def _prompt(
    phase: str,
    task: Task,
    context: list[dict],
    verify_command: str | None,
    frozen_requirements: dict[str, str],
) -> str:
    common = (
        f"あなたは独立した{phase}担当です。説明も最終JSONも日本語で記述してください。\n"
        f"タスク定義:\n{_task_text(task)}\n開始時に固定した要件原文:\n"
        f"{json.dumps(frozen_requirements, ensure_ascii=False, indent=2)}\n"
        "最終応答は指定JSON Schemaに厳密に従ってください。\n"
        "要件、受入条件、必要入力に不明点や矛盾があれば推測で補わず、statusをBLOCKEDにして"
        "不足内容をissuesへ記録してください。\n"
        "GUIや新しいコンソールを開かないでください。Windowsで補助プロセスを起動する場合は"
        "CREATE_NO_WINDOWとSW_HIDE、またはStart-Process -WindowStyle Hiddenを必ず指定してください。\n"
    )
    if phase == "実装":
        return common + "編集範囲内で要件と初期テストを実装してください。commitとpushは禁止です。"
    if phase == "修正":
        return (
            common
            + "次の指摘だけを修正してください。commitとpushは禁止です。\n"
            + json.dumps(context, ensure_ascii=False)
        )
    if phase == "テスト":
        return common + (
            "製品コードとテストコードを変更せず独立に検査してください。次の固定コマンドを一度実行し、"
            f"結果を保存してください。\n{verify_command}\n全件成功した場合だけPASSにしてください。"
        )
    return common + (
        "製品コードとテストコードを変更せず要件、差分、テスト証跡を照合してください。"
        "P0からP2は修正必須、P3は助言です。証跡: " + json.dumps(context, ensure_ascii=False)
    )


def _valid_result(value: dict | None) -> bool:
    return bool(
        value
        and value.get("status") in {"PASS", "FAIL", "BLOCKED"}
        and isinstance(value.get("summary"), str)
        and isinstance(value.get("issues"), list)
        and all(isinstance(x, str) for x in value.get("issues", []))
        and isinstance(value.get("review_findings"), list)
        and all(
            isinstance(x, dict)
            and isinstance(x.get("id"), str)
            and x.get("severity") in {"P0", "P1", "P2", "P3"}
            and isinstance(x.get("description"), str)
            for x in value.get("review_findings", [])
        )
    )


def _verification_valid(value: dict, task: Task, work: Path) -> bool:
    commands, current = value.get("commands"), code_hash(work)
    return bool(
        isinstance(commands, list)
        and len(commands) == len(task.verification_commands)
        and value.get("code_hash") == current
        and value.get("end_code_hash") == current
        and value.get("success") is True
        and all(
            a.get("argv") == list(e) and a.get("exit_code") == 0 and a.get("timed_out") is False
            for a, e in zip(commands, task.verification_commands)
        )
    )


def _integrity(runtime_parent: Path, task_path: Path, requirements_path: Path) -> dict:
    return {
        "runtime": snapshot_hashes(runtime_parent),
        "task": task_path.read_bytes().hex(),
        "requirements": requirements_path.read_bytes().hex(),
    }


def _verifier_called(phase_dir: Path) -> bool:
    events = (phase_dir / "events.jsonl").read_text(encoding="utf-8", errors="replace")
    return "dev_harness.verify_client" in events


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


def run_controller(root: Path, run_id: str) -> int:
    marker = create_marker(run_id)
    store, run_dir = Store(root), root / ".harness" / "runs" / run_id
    row = store.get(run_id)
    stop_file, task_path = run_dir / "STOP", run_dir / "task.json"
    runtime_parent, work = run_dir / "runtime", run_dir / "work"
    runtime = runtime_parent / "dev_harness"
    schema, baseline_path = runtime / "schemas" / "phase-result.json", run_dir / "baseline.json"
    integrity_path, task = run_dir / "integrity.json", Task.load(task_path)
    frozen_requirements_path = run_dir / "frozen-requirements.json"
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
    created = float(row["created"])
    decisions_path = run_dir / "control-decisions.jsonl"
    try:
        if stop_file.exists():
            store.update(
                run_id, status="STOPPED", phase=row["phase"], message="起動前に停止されました"
            )
            return 2
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
        phase_seq = (
            len(list((run_dir / "phases").glob("*"))) if (run_dir / "phases").exists() else 0
        )
        progress_path = run_dir / "progress.json"
        if progress_path.exists():
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            phase = progress["phase"]
            context = progress["context"]
            resume_spent = float(progress["spent"])
            if progress.get("started_at") is not None:
                resume_spent += max(0.0, time.time() - float(progress["started_at"]))
        else:
            phase = str(row["phase"]) if row["phase"] in PHASES else "実装"
            context: list[dict] = []
            resume_spent = 0.0
        while True:
            if stop_file.exists():
                store.update(run_id, status="STOPPED", phase=phase, message="停止指示を受けました")
                return 2
            elapsed = time.time() - created
            if elapsed >= task.timeouts.total:
                store.update(
                    run_id, status="FAILED", phase=phase, message="全体時間上限を超過しました"
                )
                return 1
            if (
                _integrity(runtime_parent, task_path, frozen_requirements_path)
                != expected_integrity
            ):
                raise RuntimeError("固定した実行環境またはタスク定義が変更されました")
            phase_seq += 1
            phase_dir = run_dir / "phases" / f"{phase_seq:02d}-{phase}"
            before = snapshot_hashes(work, task.exclude)
            verify_path = phase_dir / "verification.json"
            budget = getattr(task.timeouts, PHASES[phase])
            if resume_spent >= budget:
                store.update(
                    run_id,
                    status="FAILED",
                    phase=phase,
                    message=f"{phase}工程の時間上限を超過しました",
                )
                return 1
            timeout = min(
                max(1, int(budget - resume_spent)), max(1, int(task.timeouts.total - elapsed))
            )
            prior_spent = resume_spent
            resume_spent = 0.0
            progress_path.write_text(
                json.dumps(
                    {
                        "phase": phase,
                        "context": context,
                        "spent": prior_spent,
                        "started_at": time.time(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            broker = (
                VerificationBroker(task, work, verify_path, timeout) if phase == "テスト" else None
            )
            if broker:
                broker.start()
            verify_cmd = (
                frozen_verify_command(runtime, broker.url, broker.token) if broker else None
            )
            store.update(run_id, phase=phase, message=f"{phase}担当を実行中")
            try:
                proc, result = invoke(
                    phase,
                    _prompt(phase, task, context, verify_cmd, frozen_requirements),
                    work,
                    phase_dir,
                    schema,
                    timeout,
                    stop_file,
                )
            finally:
                if broker:
                    broker.close()
            after = snapshot_hashes(work, task.exclude)
            changed_requirements = [
                name for name in task.requirement_files if baseline.get(name) != after.get(name)
            ]
            if changed_requirements:
                store.update(
                    run_id,
                    status="FAILED",
                    phase=phase,
                    message="固定した要件ファイルが変更されました: "
                    + ", ".join(changed_requirements),
                )
                return 1
            (phase_dir / "process.json").write_text(json.dumps(vars(proc)), encoding="utf-8")
            if proc.stopped:
                progress_path.write_text(
                    json.dumps(
                        {
                            "phase": phase,
                            "context": context,
                            "spent": prior_spent + proc.duration,
                            "started_at": None,
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                store.update(run_id, status="STOPPED", phase=phase, message="停止指示を受けました")
                return 2
            if proc.timed_out:
                result = {
                    "status": "FAIL",
                    "summary": "時間切れ",
                    "issues": [f"{phase}工程が時間切れ"],
                    "review_findings": [],
                }
            elif proc.exit_code != 0 or not _valid_result(result):
                result = {
                    "status": "FAIL",
                    "summary": "工程異常",
                    "issues": [f"{phase}担当の実行または結果JSONが不正"],
                    "review_findings": [],
                }
            if phase in {"テスト", "レビュー"} and before != after:
                result = {
                    "status": "FAIL",
                    "summary": "禁止変更",
                    "issues": [f"{phase}担当が作業ファイルを変更した"],
                    "review_findings": [],
                }
            if result["status"] == "BLOCKED":
                blocked_context = [
                    *context,
                    *({"source": phase, "description": issue} for issue in result["issues"]),
                ]
                progress_path.write_text(
                    json.dumps(
                        {
                            "phase": phase,
                            "context": blocked_context,
                            "spent": prior_spent + proc.duration,
                            "started_at": None,
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                store.update(
                    run_id, status="BLOCKED", phase=phase, message="; ".join(result["issues"])
                )
                return 3
            if phase in {"実装", "修正"}:
                outside = _outside(baseline, after, task.edit_scope)
                if outside:
                    store.update(
                        run_id,
                        status="FAILED",
                        phase=phase,
                        message="編集範囲外の変更: " + ", ".join(outside),
                    )
                    return 1
                if result["status"] == "PASS":
                    phase = "テスト"
                else:
                    context = [{"source": phase, "description": x} for x in result["issues"]]
                    phase = "修正"
            elif phase == "テスト":
                evidence = None
                if verify_path.exists():
                    try:
                        evidence = json.loads(verify_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        pass
                passed = (
                    result["status"] == "PASS"
                    and broker is not None
                    and broker.result is not None
                    and evidence is not None
                    and evidence == broker.result
                    and _verification_valid(broker.result, task, work)
                    and _verifier_called(phase_dir)
                )
                if passed:
                    (run_dir / "tested-code-hash.txt").write_text(code_hash(work), encoding="ascii")
                    context = [
                        {
                            "test_evidence": str(verify_path),
                            "test_result": str(phase_dir / "result.json"),
                            "baseline": str(baseline_path),
                            "original_root": str(root),
                            "work_root": str(work),
                            "control_history": str(decisions_path),
                        }
                    ]
                    _append_decision(
                        decisions_path,
                        {"phase": "テスト", "decision": "PASS", "evidence": str(verify_path)},
                    )
                    phase = "レビュー"
                else:
                    context = [{"source": "テスト", "description": x} for x in result["issues"]]
                    failed_commands = []
                    if broker is not None and broker.result is not None:
                        failed_commands = [
                            {
                                "argv": item.get("argv"),
                                "exit_code": item.get("exit_code"),
                                "timed_out": item.get("timed_out"),
                                "stopped": item.get("stopped"),
                                "stderr": str(item.get("stderr", ""))[-2000:],
                            }
                            for item in broker.result.get("commands", [])
                            if item.get("exit_code") != 0
                            or item.get("timed_out")
                            or item.get("stopped")
                        ]
                    context.append(
                        {
                            "source": "制御",
                            "description": "固定検証が不合格です。次の実コマンド結果を修正してください。",
                            "failed_commands": failed_commands,
                            "evidence": str(verify_path),
                        }
                    )
                    _append_decision(
                        decisions_path,
                        {
                            "phase": "テスト",
                            "decision": "FAIL",
                            "agent_status": result["status"],
                            "failed_commands": failed_commands,
                            "evidence": str(verify_path),
                        },
                    )
                    phase = "修正"
            else:
                mandatory = [
                    x for x in result["review_findings"] if x["severity"] in {"P0", "P1", "P2"}
                ]
                same = (run_dir / "tested-code-hash.txt").read_text(encoding="ascii") == code_hash(
                    work
                )
                if result["status"] == "PASS" and not mandatory and same:
                    if stop_file.exists() or time.time() - created >= task.timeouts.total:
                        status = "STOPPED" if stop_file.exists() else "FAILED"
                        final_phase = "レビュー" if status == "STOPPED" else "終了"
                        if status == "STOPPED":
                            progress_path.write_text(
                                json.dumps(
                                    {
                                        "phase": "レビュー",
                                        "context": context,
                                        "spent": prior_spent + proc.duration,
                                        "started_at": None,
                                    },
                                    ensure_ascii=False,
                                ),
                                encoding="utf-8",
                            )
                        store.update(
                            run_id,
                            status=status,
                            phase=final_phase,
                            message="反映前に停止または期限へ到達しました",
                        )
                        return 2 if status == "STOPPED" else 1
                    if (
                        _integrity(runtime_parent, task_path, frozen_requirements_path)
                        != expected_integrity
                    ):
                        raise RuntimeError("固定した実行環境またはタスク定義が変更されました")
                    changes = apply_changes(root, work, baseline, task.edit_scope, task.exclude)
                    (run_dir / "changes.json").write_text(
                        json.dumps(changes, ensure_ascii=False), encoding="utf-8"
                    )
                    (run_dir / "report.md").write_text(
                        f"# 開発ハーネス実行結果\n\n状態: 完了\n\nタスク: {task.name}\n\n反映: {', '.join(changes) or 'なし'}\n",
                        encoding="utf-8",
                    )
                    store.update(
                        run_id,
                        status="COMPLETED",
                        phase="終了",
                        message="テストとレビューに合格しました",
                    )
                    return 0
                context = mandatory or [
                    {"source": "レビュー", "description": x} for x in result["issues"]
                ]
                phase = "修正"
            if phase == "修正":
                count = int(store.get(run_id)["fix_count"])
                if count >= task.max_fixes:
                    store.update(
                        run_id, status="FAILED", phase="終了", message="修正回数の上限に達しました"
                    )
                    return 1
                store.update(run_id, fix_count=count + 1, phase="修正")
            progress_path.write_text(
                json.dumps(
                    {"phase": phase, "context": context, "spent": 0.0, "started_at": None},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
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
