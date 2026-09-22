from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class TaskError(ValueError):
    pass


@dataclass(frozen=True)
class Timeouts:
    delivery: int = 14400
    verification: int = 1800
    total: int = 14400


@dataclass(frozen=True)
class Task:
    name: str
    requirements: tuple[str, ...]
    edit_scope: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    verification_commands: tuple[tuple[str, ...], ...]
    requirement_files: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    timeouts: Timeouts = field(default_factory=Timeouts)

    @classmethod
    def load(cls, path: Path) -> "Task":
        try:
            raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TaskError(f"タスク定義を読めません: {exc}") from exc
        required = (
            "name",
            "requirements",
            "edit_scope",
            "acceptance_criteria",
            "verification_commands",
        )
        allowed = {*required, "requirement_files", "exclude", "timeouts"}
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise TaskError(f"未知の項目があります: {', '.join(unknown)}")
        missing = [key for key in required if key not in raw]
        if missing:
            raise TaskError(f"必須項目がありません: {', '.join(missing)}")
        list_fields = ("requirements", "edit_scope", "acceptance_criteria")
        if any(
            not isinstance(raw[k], list)
            or not raw[k]
            or not all(isinstance(x, str) and x for x in raw[k])
            for k in list_fields
        ):
            raise TaskError("requirements、edit_scope、acceptance_criteria は空でない配列です")
        commands = raw["verification_commands"]
        if (
            not isinstance(commands, list)
            or not commands
            or any(
                not isinstance(c, list) or not c or not all(isinstance(x, str) and x for x in c)
                for c in commands
            )
        ):
            raise TaskError("verification_commands は空でないargv配列の配列です")
        for rel in [*raw["edit_scope"], *raw.get("requirement_files", []), *raw.get("exclude", [])]:
            p = Path(rel)
            if p.is_absolute() or p.drive or p.root or ".." in p.parts:
                raise TaskError(f"パスはリポジトリ内の相対パスにしてください: {rel}")
        timeout_raw = raw.get("timeouts", {})
        if not isinstance(timeout_raw, dict) or set(timeout_raw) - set(
            Timeouts.__dataclass_fields__
        ):
            raise TaskError("timeouts に未知の項目があります")
        defaults = Timeouts()
        timeouts = Timeouts(
            **{
                name: int(timeout_raw.get(name, getattr(defaults, name)))
                for name in Timeouts.__dataclass_fields__
            }
        )
        if any(v <= 0 for v in vars(timeouts).values()):
            raise TaskError("時間上限は正の秒数です")
        limits = Timeouts()
        if any(
            getattr(timeouts, key) > getattr(limits, key) for key in Timeouts.__dataclass_fields__
        ):
            raise TaskError("時間上限は既定上限を超えられません")
        return cls(
            name=str(raw["name"]),
            requirements=tuple(map(str, raw["requirements"])),
            edit_scope=tuple(map(str, raw["edit_scope"])),
            acceptance_criteria=tuple(map(str, raw["acceptance_criteria"])),
            verification_commands=tuple(tuple(c) for c in commands),
            requirement_files=tuple(map(str, raw.get("requirement_files", []))),
            exclude=tuple(map(str, raw.get("exclude", []))),
            timeouts=timeouts,
        )
