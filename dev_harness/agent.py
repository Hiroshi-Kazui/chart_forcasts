from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from .process import ProcessResult, run_process

MODELS = {
    "実装": ("gpt-5.6-sol", "medium"),
    "テスト": ("gpt-5.6-sol", "medium"),
    "レビュー": ("gpt-6-astra", "high"),
    "修正": ("gpt-5.6-sol", "medium"),
}


def _resolve_codex(command: str) -> list[str]:
    """WindowsのPowerShellラッパーも、シェル展開なしのargvへ解決する。"""
    if os.name != "nt":
        return [command]
    candidate = Path(command)
    resolved: Path | None = None
    if candidate.parent != Path(".") or candidate.suffix:
        if candidate.exists():
            resolved = candidate.resolve()
    else:
        for suffix in (".exe", ".ps1", ".cmd", ".bat", ".com"):
            found = shutil.which(command + suffix)
            if found:
                resolved = Path(found).resolve()
                break
    if resolved is None:
        found = shutil.which(command)
        if found:
            resolved = Path(found).resolve()
    if resolved is None:
        raise FileNotFoundError(f"Codex実行ファイルが見つかりません: {command}")
    suffix = resolved.suffix.lower()
    if suffix == ".ps1":
        powershell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
        if not powershell:
            raise FileNotFoundError("PowerShell実行ファイルが見つかりません")
        return [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(resolved)]
    if suffix in {".cmd", ".bat"}:
        # cmdラッパーは固定されたCodex入口に限る。可変値は後続argvとして引用される。
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", str(resolved)]
    return [str(resolved)]


def invoke(
    phase: str,
    prompt: str,
    work: Path,
    phase_dir: Path,
    schema: Path,
    timeout: int,
    stop_file: Path,
) -> tuple[ProcessResult, dict | None]:
    phase_dir.mkdir(parents=True, exist_ok=False)
    result_path = phase_dir / "result.json"
    codex = os.environ.get("DEV_HARNESS_CODEX", "codex")
    model, effort = MODELS[phase]
    argv = [
        *_resolve_codex(codex),
        "exec",
        "-C",
        str(work),
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "--json",
        "--output-schema",
        str(schema),
        "--output-last-message",
        str(result_path),
        "--model",
        model,
        "-c",
        f'model_reasoning_effort="{effort}"',
        "-",
    ]
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
    env["DEV_HARNESS_PROMPT_FILE"] = str(phase_dir / "prompt.txt")
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


def frozen_verify_command(runtime: Path, url: str, token: str) -> str:
    code = (
        "import sys;sys.path.insert(0,r'"
        + str(runtime.parent).replace("'", "''")
        + "');from dev_harness.verify_client import main;raise SystemExit(main())"
    )
    return f'"{sys.executable}" -I -c "{code}" --url "{url}" --token "{token}"'
