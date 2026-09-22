from __future__ import annotations

import ctypes
import hashlib
import os


def mutex_name(run_id: str) -> str:
    digest = hashlib.sha256(run_id.encode()).hexdigest()[:24]
    return f"Local\\chart_forecasts_harness_{digest}"


def create_marker(run_id: str):
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    handle = kernel32.CreateMutexW(None, False, mutex_name(run_id))
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateMutexW")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        raise RuntimeError("同じ実行IDの制御プロセスが既に動作しています")
    return handle


def close_marker(handle) -> None:
    if handle and os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle(handle)


def controller_alive(run_id: str, pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenMutexW.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.OpenMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenMutexW(0x00100000, False, mutex_name(run_id))
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
