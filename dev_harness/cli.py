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
from .liveness import controller_alive
from .store import Store


def _root() -> Path:
    return Path.cwd().resolve()


def _start_controller(root: Path, run_id: str, runtime_parent: Path) -> int:
    code = (
        "import sys;sys.path.insert(0,r'"
        + str(runtime_parent).replace("'", "''")
        + "');from dev_harness.controller import main;raise SystemExit(main())"
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
    shutil.copytree(Path(__file__).parent, runtime_parent / "dev_harness")
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


def command_status(args: argparse.Namespace) -> int:
    store = Store(_root())
    row = _get(store, args.run)
    if row is None:
        return 2
    stale = row["status"] == "RUNNING" or (
        row["status"] == "QUEUED" and time.time() - float(row["updated"]) > 5
    )
    if stale and not controller_alive(args.run, row["controller_pid"]):
        store.transition(
            args.run,
            (row["status"],),
            status="FAILED",
            controller_pid=None,
            message="制御プロセスが異常終了しました",
        )
        row = store.get(args.run)
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
    if args.json:
        print(json.dumps(stable, ensure_ascii=False))
    else:
        print(f"{stable['run_id']} {stable['status']} {stable['phase']} {stable['message']}")
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
    stale = row["status"] == "RUNNING" or (
        row["status"] == "QUEUED" and time.time() - float(row["updated"]) > 5
    )
    if stale and not controller_alive(args.run, row["controller_pid"]):
        store.transition(
            args.run,
            (row["status"],),
            status="FAILED",
            controller_pid=None,
            message="制御プロセスが異常終了しました",
        )
        row = store.get(args.run)
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
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
