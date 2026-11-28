"""Run the release executable without a configured runtime in an interactive Windows session."""

import argparse
import ctypes
from ctypes import wintypes as win
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import time


user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
enum_callback = ctypes.WINFUNCTYPE(win.BOOL, win.HWND, win.LPARAM)


class ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("size", win.DWORD),
        ("usage", win.DWORD),
        ("pid", win.DWORD),
        ("heap", ctypes.c_size_t),
        ("module", win.DWORD),
        ("threads", win.DWORD),
        ("parent", win.DWORD),
        ("priority", win.LONG),
        ("flags", win.DWORD),
        ("name", win.WCHAR * 260),
    ]


def windows_api(dll, name, returns, *args):
    function = getattr(dll, name)
    function.restype = returns
    function.argtypes = args
    return function


snapshot = windows_api(kernel32, "CreateToolhelp32Snapshot", win.HANDLE, win.DWORD, win.DWORD)
process_first = windows_api(kernel32, "Process32FirstW", win.BOOL, win.HANDLE, ctypes.POINTER(ProcessEntry))
process_next = windows_api(kernel32, "Process32NextW", win.BOOL, win.HANDLE, ctypes.POINTER(ProcessEntry))
close_handle = windows_api(kernel32, "CloseHandle", win.BOOL, win.HANDLE)
enum_windows = windows_api(user32, "EnumWindows", win.BOOL, enum_callback, win.LPARAM)
is_visible = windows_api(user32, "IsWindowVisible", win.BOOL, win.HWND)
window_class = windows_api(user32, "GetClassNameW", ctypes.c_int, win.HWND, win.LPWSTR, ctypes.c_int)
window_pid = windows_api(user32, "GetWindowThreadProcessId", win.DWORD, win.HWND, ctypes.POINTER(win.DWORD))
post_message = windows_api(user32, "PostMessageW", win.BOOL, win.HWND, win.UINT, win.WPARAM, win.LPARAM)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def assert_gui_subsystem(executable):
    with executable.open("rb") as stream:
        stream.seek(0x3C)
        pe_offset, = struct.unpack("<I", stream.read(4))
        stream.seek(pe_offset)
        require(stream.read(4) == b"PE\0\0", "Expected a Windows PE executable")
        stream.seek(pe_offset + 24 + 68)
        subsystem, = struct.unpack("<H", stream.read(2))
    require(subsystem == 2, "Desktop executable must use the Windows GUI subsystem")


def process_tree(root, known):
    """Track descendants across snapshots even if an intermediate process has exited."""
    handle = snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    entry = ProcessEntry()
    entry.size = ctypes.sizeof(entry)
    children = {}
    try:
        if not process_first(handle, ctypes.byref(entry)):
            raise ctypes.WinError(ctypes.get_last_error())
        while True:
            children.setdefault(entry.parent, []).append((entry.pid, entry.name.lower()))
            if not process_next(handle, ctypes.byref(entry)):
                break
    finally:
        close_handle(handle)

    pending = list(known)
    current = set()
    while pending:
        for pid, name in children.get(pending.pop(), ()):
            if pid in current or pid == root:
                continue
            require(name == "msedgewebview2.exe", f"Desktop started an unexpected process: {name}")
            current.add(pid)
            if pid not in known:
                pending.append(pid)
    known.update(current)


def visible_windows():
    windows = {}

    @enum_callback
    def record(hwnd, _):
        if is_visible(hwnd):
            name = ctypes.create_unicode_buffer(256)
            pid = win.DWORD()
            if window_class(hwnd, name, len(name)) and window_pid(hwnd, ctypes.byref(pid)):
                windows[hwnd] = (pid.value, name.value)
        return True

    if not enum_windows(record, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return windows


def run_desktop(executable, env, *, self_test, watch_seconds):
    args = [str(executable)]
    if self_test:
        args.append("--self-test")
    console_class = "ConsoleWindowClass"
    baseline = visible_windows()
    with subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as app:
        known = {app.pid}
        started = time.monotonic()
        next_process_sample = started
        saw_app = False
        try:
            while app.poll() is None:
                now = time.monotonic()
                if now >= next_process_sample:
                    process_tree(app.pid, known)
                    next_process_sample = now + 0.05
                windows = visible_windows()
                require(
                    not any(
                        name == console_class and (hwnd not in baseline or baseline[hwnd] != (pid, name))
                        for hwnd, (pid, name) in windows.items()
                    ),
                    "A new visible console window appeared during the desktop launch",
                )
                desktop_windows = [hwnd for hwnd, (pid, _) in windows.items() if pid == app.pid]
                saw_app |= bool(desktop_windows)
                elapsed = now - started
                if self_test:
                    require(elapsed < 25, "Desktop self-test did not exit")
                elif elapsed >= watch_seconds and desktop_windows:
                    require(post_message(desktop_windows[0], 0x0010, 0, 0), "Could not close the desktop window")
                    break
                elif not saw_app:
                    require(elapsed < 20, "The desktop GUI did not open")
                else:
                    require(elapsed < watch_seconds + 20, "The desktop GUI disappeared during the watch")
                time.sleep(0.01)
            stdout, stderr = app.communicate(timeout=10)
            require(app.returncode == 0, f"Desktop exited {app.returncode}: {stderr.strip()}")
            require(saw_app, "No desktop GUI window was visible")
            if not self_test:
                require(time.monotonic() - started >= watch_seconds, "Desktop exited before the watch ended")
            return stdout.strip()
        finally:
            if app.poll() is None:
                app.kill()
                app.communicate(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--seconds", type=float, default=60)
    args = parser.parse_args()
    require(math.isfinite(args.seconds) and args.seconds > 0, "Watch duration must be positive")
    executable = args.executable.resolve(strict=True)
    assert_gui_subsystem(executable)

    with tempfile.TemporaryDirectory(prefix="desktop-smoke-", ignore_cleanup_errors=True) as temporary:
        profile = Path(temporary) / "profile"
        profile.mkdir()
        env = os.environ | {
            "USERPROFILE": str(profile),
            "HOME": str(profile),
            "WEBVIEW2_USER_DATA_FOLDER": str(Path(temporary) / "webview"),
        }
        result = json.loads(run_desktop(executable, env, self_test=True, watch_seconds=0))
        require(result["ok"] is True and result["runtime_state"] == "offline", "Expected the runtime to be offline")
        require(result["offline_visible"] is True, "Expected the offline indicator to be visible")
        for field in ("first_paint_ms", "first_contentful_paint_ms", "startup_to_paint_report_ms"):
            require(math.isfinite(result[field]) and result[field] >= 0, f"Invalid timing: {field}")
        print(json.dumps(result), flush=True)

        run_desktop(executable, env, self_test=False, watch_seconds=args.seconds)
        require(not (profile / ".mco").exists(), "Desktop created runtime state in the empty profile")

    print(f"{args.seconds:g}s console window watch passed. No unexpected child process observed.")


if __name__ == "__main__":
    main()
