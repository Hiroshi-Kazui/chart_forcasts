from pathlib import Path

import pytest

from claude_harness.workspace import apply_changes, make_copy, snapshot_hashes


def test_make_copy_preserves_uncommitted_source_and_omits_runtime_state(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    (source / "src").mkdir(parents=True)
    (source / ".harness").mkdir()
    (source / "src" / "app.py").write_text("uncommitted\n", encoding="utf-8")
    (source / ".harness" / "state.sqlite3").write_bytes(b"state")

    baseline = make_copy(source, work, ())

    assert (work / "src" / "app.py").read_text(encoding="utf-8") == "uncommitted\n"
    assert not (work / ".harness").exists()
    assert baseline == snapshot_hashes(source)


def test_apply_rejects_scope_violation_without_partial_write(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    (source / "src").mkdir(parents=True)
    (source / "docs").mkdir()
    (source / "src" / "a.py").write_text("old-a", encoding="utf-8")
    (source / "docs" / "note.md").write_text("old-note", encoding="utf-8")
    baseline = make_copy(source, work, ())
    (work / "src" / "a.py").write_text("new-a", encoding="utf-8")
    (work / "docs" / "note.md").write_text("new-note", encoding="utf-8")

    with pytest.raises(RuntimeError, match="docs/note.md"):
        apply_changes(source, work, baseline, ("src",))

    assert (source / "src" / "a.py").read_text(encoding="utf-8") == "old-a"
    assert (source / "docs" / "note.md").read_text(encoding="utf-8") == "old-note"


def test_apply_rejects_source_conflict_atomically(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    (source / "src").mkdir(parents=True)
    (source / "src" / "a.py").write_text("old-a", encoding="utf-8")
    (source / "src" / "z.py").write_text("old-z", encoding="utf-8")
    baseline = make_copy(source, work, ())
    (work / "src" / "a.py").write_text("agent-a", encoding="utf-8")
    (work / "src" / "z.py").write_text("agent-z", encoding="utf-8")
    (source / "src" / "z.py").write_text("user-z", encoding="utf-8")

    with pytest.raises(RuntimeError, match="src/z.py"):
        apply_changes(source, work, baseline, ("src",))

    assert (source / "src" / "a.py").read_text(encoding="utf-8") == "old-a"
    assert (source / "src" / "z.py").read_text(encoding="utf-8") == "user-z"


def test_apply_does_not_clobber_user_file_named_like_internal_staging(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    (source / "src").mkdir(parents=True)
    (source / "src" / "a.txt").write_text("old", encoding="utf-8")
    (source / "src" / "a.txt.harness-new").write_text("user data", encoding="utf-8")
    baseline = make_copy(source, work, ())
    (work / "src" / "a.txt").write_text("new", encoding="utf-8")

    apply_changes(source, work, baseline, ("src/a.txt",))

    assert (source / "src" / "a.txt").read_text(encoding="utf-8") == "new"
    assert (source / "src" / "a.txt.harness-new").read_text(encoding="utf-8") == "user data"


def test_excluded_file_is_never_applied(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    (source / "src").mkdir(parents=True)
    (source / "src" / "app.py").write_text("old", encoding="utf-8")
    baseline = make_copy(source, work, ("src/generated.txt",))
    (work / "src" / "app.py").write_text("new", encoding="utf-8")
    (work / "src" / "generated.txt").write_text("do not apply", encoding="utf-8")

    changed = apply_changes(source, work, baseline, ("src",), ("src/generated.txt",))

    assert changed == ["src/app.py"]
    assert (source / "src" / "app.py").read_text(encoding="utf-8") == "new"
    assert not (source / "src" / "generated.txt").exists()


def test_snapshot_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside.txt"
    root.mkdir()
    outside.write_text("secret", encoding="utf-8")
    link = root / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(RuntimeError, match="link.txt"):
        snapshot_hashes(root)
