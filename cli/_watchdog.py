"""Parent-death watchdog: force-exit modkit when its launcher goes away.

On Windows, killing a process does not kill its children. When an agent kills
the shell that launched modkit, modkit.exe survives as an orphan and keeps its
native workers (conversion, LOD gen, texture batches) running, holding gigabytes
of RAM. This watchdog exits modkit as soon as the original parent exits.

It watches the parent rather than stdin EOF because some commands read piped
stdin (`esp set-record -`), and a stdin watchdog would race them for input.
"""
from __future__ import annotations

import os
import sys
import threading


def _default_exit(code: int) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os._exit(code)


if sys.platform == "win32":
    import ctypes

    _kernel32 = ctypes.windll.kernel32
    _SYNCHRONIZE = 0x00100000

    _kernel32.OpenProcess.restype = ctypes.c_void_p
    _kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    _kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    _kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    def _wait_for_process_exit(pid: int) -> bool:
        """Block until `pid` exits. Return True if its exit was observed, False
        if the process could not be watched (already gone / no access)."""
        handle = _kernel32.OpenProcess(_SYNCHRONIZE, False, int(pid))
        if not handle:
            return False
        try:
            _kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)  # INFINITE
            return True
        finally:
            _kernel32.CloseHandle(handle)

else:
    import time as _time

    def _wait_for_process_exit(pid: int) -> bool:
        pid = int(pid)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            pass  # exists but e.g. not permitted to signal -- still watchable
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except OSError:
                pass
            _time.sleep(0.25)


def install_parent_death_watchdog(on_exit=None, parent_pid: int | None = None):
    """Start a daemon thread that force-exits when the parent process dies.

    Returns the started thread, or None if there is no valid parent to watch.
    `on_exit(code)` defaults to a flush-and-os._exit; it is only ever called
    when the parent's exit is actually observed, never on a failure to watch.
    """
    if on_exit is None:
        on_exit = _default_exit
    if parent_pid is None:
        try:
            parent_pid = os.getppid()
        except Exception:
            return None
    if not parent_pid or int(parent_pid) <= 0:
        return None

    def _watch() -> None:
        if _wait_for_process_exit(parent_pid):
            on_exit(0)

    thread = threading.Thread(target=_watch, name="parent-death-watchdog", daemon=True)
    thread.start()
    return thread
