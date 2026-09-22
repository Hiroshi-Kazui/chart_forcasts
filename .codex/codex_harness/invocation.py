from __future__ import annotations

import os
import shutil
from pathlib import Path

# 受注側の設定。どのモデルをどの強度で使い、内部をどう進めるかは受注側が決める。
MODEL = "gpt-5.6-sol"
EFFORT = "high"
COMMAND_ENV = "DEV_HARNESS_CODEX"
PROMPT_ENV = "DEV_HARNESS_PROMPT_FILE"


def schema_path(runtime_parent: Path) -> Path:
    return runtime_parent / "codex_harness" / "schemas" / "delivery-result.json"


def resolve_codex(command: str) -> list[str]:
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


def build_argv(
    work: Path, schema: Path, result_path: Path, command: str | None = None
) -> tuple[list[str], str, str]:
    """Codex CLIへ渡すargvを組み立てる。"""
    model, effort = MODEL, EFFORT
    codex = command or os.environ.get(COMMAND_ENV, "codex")
    argv = [
        *resolve_codex(codex),
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
    return argv, model, effort
