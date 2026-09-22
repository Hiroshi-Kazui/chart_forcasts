from __future__ import annotations

import json
import os
from pathlib import Path

from codex_harness.invocation import PROMPT_ENV, build_argv

from .process import ProcessResult, run_process


def invoke(
    prompt: str,
    work: Path,
    phase_dir: Path,
    schema: Path,
    timeout: int,
    stop_file: Path,
) -> tuple[ProcessResult, dict | None]:
    phase_dir.mkdir(parents=True, exist_ok=False)
    result_path = phase_dir / "result.json"
    argv, model, effort = build_argv(work, schema, result_path)
    (phase_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    (phase_dir / "invocation.json").write_text(
        json.dumps(
            {
                "argv": argv,
                "cwd": str(work),
                "model": model,
                "reasoning_effort": effort,
                "timeout": timeout,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env[PROMPT_ENV] = str(phase_dir / "prompt.txt")
    with (
        (phase_dir / "prompt.txt").open("rb") as inp,
        (phase_dir / "events.jsonl").open("wb") as out,
        (phase_dir / "stderr.log").open("wb") as err,
    ):
        proc_result = run_process(argv, work, timeout, out, err, stop_file, env, inp)
    parsed = None
    if result_path.exists():
        try:
            candidate = json.loads(result_path.read_text(encoding="utf-8"))
            if isinstance(candidate, dict):
                parsed = candidate
        except json.JSONDecodeError:
            pass
    return proc_result, parsed
