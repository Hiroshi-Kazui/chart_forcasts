from __future__ import annotations

import json
from pathlib import Path

from .verify import code_hash

REVIEWER_MODEL = "claude-fable-5-1"
MANDATORY = {"P0", "P1", "P2"}
SEVERITIES = {"P0", "P1", "P2", "P3"}
AWAITING = "AWAITING_FINAL_REVIEW"


def valid_verdict(value: object) -> bool:
    """判定JSONが固定スキーマに適合するかを、依存を増やさず検査する。"""
    if not isinstance(value, dict):
        return False
    if set(value) - {"status", "summary", "findings", "reviewer"}:
        return False
    if value.get("status") not in {"PASS", "FAIL"}:
        return False
    if not isinstance(value.get("summary"), str) or not value["summary"].strip():
        return False
    findings = value.get("findings")
    if not isinstance(findings, list):
        return False
    for item in findings:
        if not isinstance(item, dict) or set(item) - {"id", "severity", "description", "evidence"}:
            return False
        if not isinstance(item.get("id"), str) or not item["id"].strip():
            return False
        if item.get("severity") not in SEVERITIES:
            return False
        if not isinstance(item.get("description"), str) or not item["description"].strip():
            return False
        if not isinstance(item.get("evidence"), str) or not item["evidence"].strip():
            return False
    reviewer = value.get("reviewer")
    if not isinstance(reviewer, dict) or set(reviewer) - {"model", "session"}:
        return False
    if not isinstance(reviewer.get("model"), str):
        return False
    if "session" in reviewer and not isinstance(reviewer["session"], str):
        return False
    return True


def blocking_findings(verdict: dict) -> list[dict]:
    return [x for x in verdict.get("findings", []) if x["severity"] in MANDATORY]


def _under(path_text: str, run_dir: Path, root: Path) -> bool:
    candidate = Path(path_text)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        return candidate.resolve().is_relative_to(run_dir.resolve())
    except (OSError, ValueError):
        return False


def check_verdict(
    verdict: object, run_dir: Path, root: Path, work: Path, tested_hash_path: Path
) -> list[str]:
    """制御側が判定を受理してよいかを検査し、問題点を日本語で返す。"""
    if not valid_verdict(verdict):
        return ["判定JSONが固定スキーマに適合しません"]
    assert isinstance(verdict, dict)
    problems: list[str] = []
    if verdict["reviewer"]["model"] != REVIEWER_MODEL:
        problems.append(
            f"最終レビュー担当は{REVIEWER_MODEL}に固定です: {verdict['reviewer']['model']}"
        )
    for item in verdict["findings"]:
        if not _under(item["evidence"], run_dir, root):
            problems.append(
                f"指摘{item['id']}の証跡が実行証跡ディレクトリ外です: {item['evidence']}"
            )
    if not tested_hash_path.exists():
        problems.append("テスト時のコードハッシュ記録がありません")
    elif tested_hash_path.read_text(encoding="ascii").strip() != code_hash(work):
        problems.append("作業コピーがテスト時から変更されています")
    if verdict["status"] == "PASS" and blocking_findings(verdict):
        problems.append("PASSにP0からP2の指摘を含めることはできません")
    return problems


def load_verdict(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"判定JSONを読めません: {exc}") from exc
