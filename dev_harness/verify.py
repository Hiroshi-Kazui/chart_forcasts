from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import time
from pathlib import Path

from .config import Task
from .workspace import snapshot_hashes
from .process import run_process


def code_hash(root: Path) -> str:
    payload = json.dumps(snapshot_hashes(root), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def execute(
    task: Task, root: Path, output: Path, command_timeout: int = 900, stop_file: Path | None = None
) -> dict:
    revision = code_hash(root)
    runs = []
    for argv in task.verification_commands:
        if stop_file is not None and stop_file.exists():
            runs.append(
                {
                    "argv": list(argv),
                    "exit_code": None,
                    "timed_out": False,
                    "stopped": True,
                    "stdout": "",
                    "stderr": "未実行: 停止指示",
                    "started": time.time(),
                    "duration": 0.0,
                }
            )
            continue
        started = time.time()
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            result = run_process(list(argv), root, command_timeout, out, err, stop_file)
            out.seek(0)
            err.seek(0)
            item = {
                "argv": list(argv),
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "stopped": result.stopped,
                "stdout": out.read().decode("utf-8", errors="replace"),
                "stderr": err.read().decode("utf-8", errors="replace"),
                "started": started,
                "duration": result.duration,
            }
        runs.append(item)
    record = {
        "code_hash": revision,
        "end_code_hash": code_hash(root),
        "commands": runs,
        "success": all(r["exit_code"] == 0 and not r["timed_out"] for r in runs),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="タスク定義に固定された検証コマンドを実行します")
    p.add_argument("--task", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--root", type=Path, default=Path.cwd())
    args = p.parse_args(argv)
    record = execute(Task.load(args.task.resolve()), args.root.resolve(), args.output.resolve())
    return 0 if record["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
