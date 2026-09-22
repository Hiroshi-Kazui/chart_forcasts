from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    timed_out: bool
    stopped: bool
    duration: float


class WindowsJob:
    """WindowsではKILL_ON_JOB_CLOSEを設定し、子孫をまとめて終了する。"""

    def __init__(self) -> None:
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32 = kernel32
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW")

        class BASIC_LIMIT(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                (n, ctypes.c_uint64)
                for n in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMIT),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(handle)
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject")
        self.handle = handle

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        self.assign_handle(int(process._handle))  # type: ignore[attr-defined]

    def assign_handle(self, process_handle: int) -> None:
        if self.handle is not None and not self._kernel32.AssignProcessToJobObject(
            self.handle,
            process_handle,
        ):
            raise OSError("AssignProcessToJobObject")

    def terminate(self) -> None:
        if self.handle is not None:
            self._kernel32.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        if self.handle is not None:
            self._kernel32.CloseHandle(self.handle)
            self.handle = None


def run_process(
    argv: list[str],
    cwd: Path,
    timeout: int,
    stdout: BinaryIO,
    stderr: BinaryIO,
    stop_file: Path | None = None,
    env: dict[str, str] | None = None,
    stdin: BinaryIO | None = None,
) -> ProcessResult:
    start = time.monotonic()
    if os.name == "nt":
        return _run_windows_suspended(
            argv, cwd, timeout, stdout, stderr, stop_file, env, stdin, start
        )
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    job = WindowsJob()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            env=env,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
        try:
            job.assign(proc)
        except Exception:
            proc.kill()
            proc.wait()
            raise
        timed_out = stopped = False
        while proc.poll() is None:
            if stop_file and stop_file.exists():
                stopped = True
                break
            if time.monotonic() - start >= timeout:
                timed_out = True
                break
            time.sleep(0.2)
        if timed_out or stopped:
            if os.name == "nt":
                job.terminate()
            else:
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        return ProcessResult(
            proc.returncode if proc.returncode is not None else 1,
            timed_out,
            stopped,
            time.monotonic() - start,
        )
    finally:
        job.close()


def _run_windows_suspended(
    argv: list[str],
    cwd: Path,
    timeout: int,
    stdout: BinaryIO,
    stderr: BinaryIO,
    stop_file: Path | None,
    env: dict[str, str] | None,
    stdin: BinaryIO | None,
    start: float,
) -> ProcessResult:
    import _winapi
    import ctypes
    import msvcrt

    job = WindowsJob()
    devnull = None
    hp = ht = None
    handles: list[int] = []
    try:
        if stdin is None:
            devnull = open(os.devnull, "rb")
            stdin = devnull
        handles = [msvcrt.get_osfhandle(file.fileno()) for file in (stdin, stdout, stderr)]
        for handle in handles:
            os.set_handle_inheritable(handle, True)
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESTDHANDLES | subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        startup.hStdInput, startup.hStdOutput, startup.hStdError = handles
        flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
            | 0x00000004  # CREATE_SUSPENDED
        )
        hp, ht, _pid, _tid = _winapi.CreateProcess(
            None,
            subprocess.list2cmdline(argv),
            None,
            None,
            True,
            flags,
            env,
            str(cwd),
            startup,
        )
        try:
            job.assign_handle(hp)
        except Exception:
            _winapi.TerminateProcess(hp, 1)
            raise
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.ResumeThread.argtypes = [ctypes.c_void_p]
        kernel32.ResumeThread.restype = ctypes.c_uint32
        if kernel32.ResumeThread(ht) == 0xFFFFFFFF:
            _winapi.TerminateProcess(hp, 1)
            raise OSError(ctypes.get_last_error(), "ResumeThread")
        timed_out = stopped = False
        while _winapi.WaitForSingleObject(hp, 0) == _winapi.WAIT_TIMEOUT:
            if stop_file and stop_file.exists():
                stopped = True
                break
            if time.monotonic() - start >= timeout:
                timed_out = True
                break
            time.sleep(0.2)
        if timed_out or stopped:
            job.terminate()
            _winapi.WaitForSingleObject(hp, 5000)
        return ProcessResult(
            _winapi.GetExitCodeProcess(hp), timed_out, stopped, time.monotonic() - start
        )
    finally:
        for handle in handles:
            try:
                os.set_handle_inheritable(handle, False)
            except OSError:
                pass
        if ht is not None:
            _winapi.CloseHandle(ht)
        if hp is not None:
            _winapi.CloseHandle(hp)
        if devnull is not None:
            devnull.close()
        job.close()
