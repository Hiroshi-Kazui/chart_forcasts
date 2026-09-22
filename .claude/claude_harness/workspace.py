from __future__ import annotations

import fnmatch
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

# 検収中にテストやlintを走らせても納品物のハッシュが変わらないよう、ツールのキャッシュも除く。
DEFAULT_EXCLUDES = (
    ".git",
    ".harness",
    ".venv",
    ".env",
    "data",
    "runs",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
)


def _excluded(rel: Path, patterns: tuple[str, ...]) -> bool:
    posix = rel.as_posix()
    return any(part in DEFAULT_EXCLUDES for part in rel.parts) or any(
        fnmatch.fnmatch(posix, p) for p in patterns
    )


def snapshot_hashes(root: Path, excludes: tuple[str, ...] = ()) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise RuntimeError(f"シンボリックリンクは扱えません: {rel}")
        if path.is_file() and not _excluded(rel, excludes):
            result[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def make_copy(source: Path, target: Path, excludes: tuple[str, ...]) -> dict[str, str]:
    before = snapshot_hashes(source, excludes)
    target.mkdir(parents=True, exist_ok=False)
    for rel in before:
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, dst)
    if snapshot_hashes(source, excludes) != before:
        raise RuntimeError("作業コピー作成中に元ファイルが変更されました")
    return before


def changed_files(before: dict[str, str], work: Path) -> set[str]:
    after = snapshot_hashes(work)
    return {p for p in set(before) | set(after) if before.get(p) != after.get(p)}


def apply_changes(
    source: Path,
    work: Path,
    baseline: dict[str, str],
    allowed: tuple[str, ...],
    excludes: tuple[str, ...] = (),
) -> list[str]:
    current = snapshot_hashes(source, excludes)
    conflicts = [p for p in set(baseline) | set(current) if baseline.get(p) != current.get(p)]
    if conflicts:
        raise RuntimeError("開始後に元ファイルが変更されました: " + ", ".join(sorted(conflicts)))
    after = snapshot_hashes(work, excludes)
    changes = {p for p in set(baseline) | set(after) if baseline.get(p) != after.get(p)}
    disallowed = [
        p
        for p in changes
        if not any(
            fnmatch.fnmatch(p, pat) or p == pat or p.startswith(pat.rstrip("/") + "/")
            for pat in allowed
        )
    ]
    if disallowed:
        raise RuntimeError("編集範囲外の変更があります: " + ", ".join(sorted(disallowed)))
    backups: dict[str, bytes | None] = {}
    with tempfile.TemporaryDirectory(prefix="dev-harness-stage-") as staging_name:
        staging = Path(staging_name)
        for rel in sorted(changes):
            src, dst = work / rel, source / rel
            backups[rel] = dst.read_bytes() if dst.exists() else None
            if src.exists():
                stage = staging / rel
                stage.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, stage)
                if hashlib.sha256(stage.read_bytes()).hexdigest() != after[rel]:
                    raise RuntimeError(f"反映準備中に作業ファイルが変更されました: {rel}")
        # staging中に原本が変わっていないことを反映直前にも検査する。
        if snapshot_hashes(source, excludes) != current:
            raise RuntimeError("反映準備中に元ファイルが変更されました")
        try:
            for rel in sorted(changes):
                dst, stage = source / rel, staging / rel
                if stage.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(stage, dst)
                elif dst.exists():
                    dst.unlink()
        except Exception:
            for rel, content in backups.items():
                dst = source / rel
                if content is None:
                    if dst.exists():
                        dst.unlink()
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(content)
            raise
    return sorted(changes)


def write_baseline(path: Path, hashes: dict[str, str]) -> None:
    path.write_text(json.dumps(hashes, ensure_ascii=False, indent=2), encoding="utf-8")
