from __future__ import annotations

import json

from claude_harness.config import Task


def task_text(task: Task) -> str:
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


def build_prompt(task: Task, frozen_requirements: dict[str, str]) -> str:
    """一度の発注で渡す指示。進め方は受注側が決める。"""
    lines = [
        "次の発注を完了させてください。説明も最終JSONも日本語で記述してください。",
        "発注内容:",
        task_text(task),
        "開始時に固定した要件原文:",
        json.dumps(frozen_requirements, ensure_ascii=False, indent=2),
        "編集してよいのは edit_scope の範囲だけです。要件原文のファイルは変更できません。",
        "acceptance_criteria を満たし、verification_commands が全て成功する状態で納品してください。",
        "検証は納品後に発注元が同じargvで独立に実行します。作業の進め方、テストの書き方、"
        "自己レビューの有無は受注側の裁量です。",
        "要件、受入条件、必要入力に不明点や矛盾があれば推測で補わず、statusをBLOCKEDにして"
        "不足内容をissuesへ記録してください。",
        "commitとpushは禁止です。GUIや新しいコンソールを開かないでください。Windowsで補助プロセスを"
        "起動する場合はCREATE_NO_WINDOWとSW_HIDE、またはStart-Process -WindowStyle Hiddenを"
        "必ず指定してください。",
        "最終応答は指定JSON Schemaに厳密に従ってください。",
    ]
    return "\n".join(lines) + "\n"
