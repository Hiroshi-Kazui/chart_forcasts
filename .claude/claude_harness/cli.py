from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .config import Task, TaskError
from .controller import write_progress
from .final_review import AWAITING, blocking_findings, check_verdict, load_verdict
from .liveness import controller_alive
from .store import Store


def _root() -> Path:
    return Path.cwd().resolve()


def _codex_package() -> Path:
    """発注先側ハーネス（.codex）の位置を、実行時複製と開発時の双方で解決する。"""
    import codex_harness

    return Path(codex_harness.__file__).resolve().parent


def _start_controller(root: Path, run_id: str, runtime_parent: Path) -> int:
    code = (
        "import sys;sys.path.insert(0,r'"
        + str(runtime_parent).replace("'", "''")
        + "');from claude_harness.controller import main;raise SystemExit(main())"
    )
    argv = [sys.executable, "-I", "-c", code, str(root), run_id]
    flags = 0
    kwargs = {}
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        kwargs["creationflags"] = flags
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = startup
    else:
        kwargs["start_new_session"] = True
    log = root / ".harness" / "runs" / run_id / "controller.log"
    with log.open("ab") as f:
        proc = subprocess.Popen(
            argv, cwd=root, stdin=subprocess.DEVNULL, stdout=f, stderr=f, **kwargs
        )
    return proc.pid


def command_run(args: argparse.Namespace) -> int:
    root, task_path = _root(), args.task.resolve()
    try:
        task_bytes = task_path.read_bytes()
        Task.load(task_path)
        if task_path.read_bytes() != task_bytes:
            raise TaskError("検証中にタスク定義が変更されました")
    except TaskError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    store = Store(root)
    run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    run_dir = root / ".harness" / "runs" / run_id
    runtime_parent = run_dir / "runtime"
    runtime_parent.mkdir(parents=True)
    for package in (Path(__file__).resolve().parent, _codex_package()):
        shutil.copytree(package, runtime_parent / package.name)
    (run_dir / "task.json").write_bytes(task_bytes)
    try:
        store.create(run_id, task_path)
    except Exception as exc:
        if "UNIQUE" in str(exc):
            print("同じタスクが実行中です", file=sys.stderr)
            return 2
        raise
    pid = _start_controller(root, run_id, runtime_parent)
    store.transition(run_id, ("QUEUED",), controller_pid=pid)
    print(run_id)
    return 0


def _get(store: Store, run_id: str) -> dict | None:
    try:
        return store.get(run_id)
    except KeyError:
        print(f"実行IDが見つかりません: {run_id}", file=sys.stderr)
        return None


def _reconcile(store: Store, run_id: str, row: dict) -> dict:
    stale = row["status"] == "RUNNING" or (
        row["status"] == "QUEUED" and time.time() - float(row["updated"]) > 5
    )
    if stale and not controller_alive(run_id, row["controller_pid"]):
        store.transition(
            run_id,
            (row["status"],),
            status="FAILED",
            controller_pid=None,
            message="制御プロセスが異常終了しました",
        )
        row = store.get(run_id)
    return row


def _print_status(row: dict, as_json: bool) -> None:
    stable = {
        k: row[k]
        for k in (
            "id",
            "status",
            "phase",
            "fix_count",
            "message",
            "controller_pid",
            "created",
            "updated",
        )
    }
    stable["run_id"] = stable.pop("id")
    if as_json:
        print(json.dumps(stable, ensure_ascii=False))
    else:
        print(f"{stable['run_id']} {stable['status']} {stable['phase']} {stable['message']}")


def command_status(args: argparse.Namespace) -> int:
    store = Store(_root())
    row = _get(store, args.run)
    if row is None:
        return 2
    _print_status(_reconcile(store, args.run, row), args.json)
    return 0


def command_wait(args: argparse.Namespace) -> int:
    """制御プロセスが工程を進めている間だけ待ち、判断が必要な状態で戻る。"""
    store = Store(_root())
    deadline = None if args.timeout is None else time.monotonic() + args.timeout
    while True:
        row = _get(store, args.run)
        if row is None:
            return 2
        row = _reconcile(store, args.run, row)
        if row["status"] not in {"QUEUED", "RUNNING"}:
            _print_status(row, args.json)
            return 0
        if deadline is not None and time.monotonic() >= deadline:
            print("待機時間の上限に達しました", file=sys.stderr)
            return 3
        time.sleep(args.interval)


def command_final_review(args: argparse.Namespace) -> int:
    root, store = _root(), Store(_root())
    row = _get(store, args.run)
    if row is None:
        return 2
    if row["status"] != AWAITING:
        print(f"最終レビューを受け付けられる状態ではありません: {row['status']}", file=sys.stderr)
        return 2
    run_dir = root / ".harness" / "runs" / args.run
    work, progress_path = run_dir / "work", run_dir / "progress.json"
    try:
        verdict = load_verdict(args.file.resolve())
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    problems = check_verdict(verdict, run_dir, root, work, run_dir / "tested-code-hash.txt")
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 2
    assert isinstance(verdict, dict)
    task = Task.load(run_dir / "task.json")
    (run_dir / "final-review.json").write_text(
        json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    awaiting_since = float(progress.get("awaiting_since") or time.time())
    excluded = float(row["excluded_seconds"]) + max(0.0, time.time() - awaiting_since)
    blocking = blocking_findings(verdict)
    with (run_dir / "control-decisions.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "time": time.time(),
                    "phase": "最終レビュー",
                    "decision": verdict["status"],
                    "blocking": [x["id"] for x in blocking],
                    "reviewer": verdict["reviewer"]["model"],
                    "evidence": str(run_dir / "final-review.json"),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    values: dict = {
        "status": "QUEUED",
        "controller_pid": None,
        "excluded_seconds": excluded,
    }
    if verdict["status"] == "PASS" and not blocking:
        phase, context = "反映", list(progress.get("context", []))
    else:
        count = int(row["fix_count"])
        if count >= task.max_fixes:
            store.update(
                args.run,
                status="FAILED",
                phase="終了",
                message="修正回数の上限に達しました",
                excluded_seconds=excluded,
            )
            print("修正回数の上限に達しました", file=sys.stderr)
            return 1
        phase = "修正"
        context = [
            {
                "source": "最終レビュー",
                "description": f"[{x['severity']}] {x['description']}",
                "evidence": x["evidence"],
            }
            for x in (blocking or verdict["findings"])
        ] or [{"source": "最終レビュー", "description": verdict["summary"]}]
        values["fix_count"] = count + 1
    values["phase"] = phase
    values["message"] = "最終レビューの判定を受理しました"
    write_progress(progress_path, phase, context, 0.0)
    if not store.transition(args.run, (AWAITING,), **values):
        print("別の処理が先に状態を変更しました", file=sys.stderr)
        return 2
    try:
        pid = _start_controller(root, args.run, run_dir / "runtime")
    except Exception:
        store.update(args.run, status="FAILED", message="制御処理を起動できませんでした")
        raise
    store.transition(args.run, ("QUEUED",), controller_pid=pid)
    print(args.run)
    return 0


def command_stop(args: argparse.Namespace) -> int:
    root, store = _root(), Store(_root())
    row = _get(store, args.run)
    if row is None:
        return 2
    if row["status"] not in {"QUEUED", "RUNNING"}:
        print("実行中ではありません", file=sys.stderr)
        return 2
    (root / ".harness" / "runs" / args.run / "STOP").touch()
    print("停止を要求しました")
    return 0


def command_resume(args: argparse.Namespace) -> int:
    root, store = _root(), Store(_root())
    row = _get(store, args.run)
    if row is None:
        return 2
    row = _reconcile(store, args.run, row)
    if row["status"] == AWAITING:
        print(
            "最終レビュー待ちです。final-reviewで判定を提出してください",
            file=sys.stderr,
        )
        return 2
    if row["status"] not in {"STOPPED", "FAILED", "BLOCKED"}:
        print("再開できる状態ではありません", file=sys.stderr)
        return 2
    run_dir = root / ".harness" / "runs" / args.run
    if row["phase"] == "終了":
        print("終了済みまたは修正上限に達した実行は再開できません", file=sys.stderr)
        return 2
    stop = run_dir / "STOP"
    if stop.exists():
        stop.unlink()
    if not store.transition(
        args.run,
        ("STOPPED", "FAILED", "BLOCKED"),
        status="QUEUED",
        controller_pid=None,
        message="再開待ち",
    ):
        print("別の制御処理が先に再開しました", file=sys.stderr)
        return 2
    try:
        pid = _start_controller(root, args.run, run_dir / "runtime")
    except Exception:
        store.update(args.run, status="FAILED", message="制御処理を起動できませんでした")
        raise
    store.transition(args.run, ("QUEUED",), controller_pid=pid)
    print(args.run)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="サブエージェント開発ハーネス")
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--task", required=True, type=Path)
    run.set_defaults(func=command_run)
    status = sub.add_parser("status")
    status.add_argument("--run", required=True)
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=command_status)
    stop = sub.add_parser("stop")
    stop.add_argument("--run", required=True)
    stop.set_defaults(func=command_stop)
    resume = sub.add_parser("resume")
    resume.add_argument("--run", required=True)
    resume.set_defaults(func=command_resume)
    wait = sub.add_parser("wait")
    wait.add_argument("--run", required=True)
    wait.add_argument("--json", action="store_true")
    wait.add_argument("--interval", type=float, default=1.0)
    wait.add_argument("--timeout", type=float)
    wait.set_defaults(func=command_wait)
    final_review = sub.add_parser("final-review")
    final_review.add_argument("--run", required=True)
    final_review.add_argument("--file", required=True, type=Path)
    final_review.set_defaults(func=command_final_review)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
