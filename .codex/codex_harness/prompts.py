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


def build_prompt(
    phase: str,
    task: Task,
    context: list[dict],
    verify_command: str | None,
    frozen_requirements: dict[str, str],
) -> str:
    common = (
        f"あなたは独立した{phase}担当です。説明も最終JSONも日本語で記述してください。\n"
        f"タスク定義:\n{task_text(task)}\n開始時に固定した要件原文:\n"
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
