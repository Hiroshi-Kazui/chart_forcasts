import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claude_harness.process import run_process


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            startupinfo=startupinfo,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return str(pid) in completed.stdout
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_timeout_terminates_descendant_process(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    script = tmp_path / "parent.py"
    script.write_text(
        "import os, pathlib, subprocess, sys, time\n"
        "flags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0\n"
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], creationflags=flags)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    with (tmp_path / "stdout").open("wb") as stdout, (tmp_path / "stderr").open("wb") as stderr:
        result = run_process(
            [sys.executable, str(script), str(pid_file)],
            tmp_path,
            timeout=1,
            stdout=stdout,
            stderr=stderr,
        )

    assert result.timed_out
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _pid_exists(child_pid):
        time.sleep(0.1)
    assert not _pid_exists(child_pid), f"descendant PID {child_pid} survived timeout"


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object regression")
def test_job_assignment_failure_happens_before_process_can_spawn_descendant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import claude_harness.process as process_module

    pid_file = tmp_path / "child.pid"
    script = tmp_path / "parent.py"
    script.write_text(
        "import os, pathlib, subprocess, sys, time\n"
        "flags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0\n"
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], creationflags=flags)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    def fail_assignment(job: object, process_handle: int) -> None:
        raise OSError("forced AssignProcessToJobObject failure")

    monkeypatch.setattr(process_module.WindowsJob, "assign_handle", fail_assignment)
    with (tmp_path / "stdout").open("wb") as stdout, (tmp_path / "stderr").open("wb") as stderr:
        with pytest.raises(OSError, match="forced"):
            run_process(
                [sys.executable, str(script), str(pid_file)],
                tmp_path,
                timeout=10,
                stdout=stdout,
                stderr=stderr,
            )
    time.sleep(0.2)
    assert not pid_file.exists(), "suspended process ran before successful Job assignment"


@pytest.mark.skipif(os.name != "nt", reason="Windows visibility regression")
def test_windows_child_has_no_console_and_does_not_take_foreground(tmp_path: Path) -> None:
    import ctypes

    observation = tmp_path / "window.txt"
    foreground_before = ctypes.windll.user32.GetForegroundWindow()
    script = (
        "import ctypes,pathlib,sys,time;"
        "pathlib.Path(sys.argv[1]).write_text(str(ctypes.windll.kernel32.GetConsoleWindow()));"
        "time.sleep(0.2)"
    )
    with (tmp_path / "stdout").open("wb") as stdout, (tmp_path / "stderr").open("wb") as stderr:
        result = run_process(
            [sys.executable, "-c", script, str(observation)],
            tmp_path,
            timeout=5,
            stdout=stdout,
            stderr=stderr,
        )
    foreground_after = ctypes.windll.user32.GetForegroundWindow()

    assert result.exit_code == 0
    assert observation.read_text(encoding="utf-8") == "0"
    assert foreground_after == foreground_before
