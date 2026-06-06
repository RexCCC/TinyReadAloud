"""TinyReadAloud launcher — file logging and single-instance guard."""

import ctypes
import os
import subprocess
import sys

import bootstrap

bootstrap.configure_runtime()

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MB_OK = 0x0
MB_ICONINFORMATION = 0x40
MB_ICONERROR = 0x10
_APP_MARKER = "TinyReadAloud"


def _data_dir():
    base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~\\AppData\\Local"))
    path = os.path.join(base, "TinyReadAloud")
    os.makedirs(path, exist_ok=True)
    return path


def _lock_path():
    return os.path.join(_data_dir(), ".instance.lock")


def _pid_running(pid):
    handle = ctypes.windll.kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def _is_tinyreadaloud_process(pid):
    """True only if this PID is actually our pythonw/python running run.py."""
    if pid == os.getpid():
        return True
    if not _pid_running(pid):
        return False
    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' "
                f"-ErrorAction SilentlyContinue).CommandLine",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        cmd = (result.stdout or "").strip()
        return _APP_MARKER in cmd and "run.py" in cmd
    except Exception:
        return False


def _clear_stale_lock():
    path = _lock_path()
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            old_pid = int(f.read().strip())
        if _is_tinyreadaloud_process(old_pid):
            return
    except (ValueError, OSError):
        pass
    try:
        os.remove(path)
    except OSError:
        pass


def _notify_already_running():
    ctypes.windll.user32.MessageBoxW(
        0,
        "TinyReadAloud is already running.\n\n"
        "Look for the icon in the system tray (click ^ near the clock).\n\n"
        "To force a fresh start, run reset-and-launch.vbs in the TinyReadAloud folder.",
        "TinyReadAloud",
        MB_OK | MB_ICONINFORMATION,
    )


def _notify_startup_error(log_path):
    ctypes.windll.user32.MessageBoxW(
        0,
        f"TinyReadAloud failed to start.\n\nSee log:\n{log_path}",
        "TinyReadAloud Error",
        MB_OK | MB_ICONERROR,
    )


def _acquire_instance_lock():
    _clear_stale_lock()
    path = _lock_path()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                old_pid = int(f.read().strip())
            if _is_tinyreadaloud_process(old_pid):
                return False
        except (ValueError, OSError):
            pass
        try:
            os.remove(path)
        except OSError:
            return False
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def _release_instance_lock():
    path = _lock_path()
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                if f.read().strip() == str(os.getpid()):
                    os.remove(path)
    except OSError:
        pass


def main():
    log_path = os.path.join(_data_dir(), "tinyreadaloud.log")
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = log_file
    sys.stderr = log_file
    print(f"\n--- TinyReadAloud start pid={os.getpid()} ---", flush=True)

    if not _acquire_instance_lock():
        print("Another instance is already running; exiting.", flush=True)
        _notify_already_running()
        return

    try:
        import app
        app.main()
    except Exception:
        import traceback
        traceback.print_exc()
        _notify_startup_error(log_path)
        raise
    finally:
        _release_instance_lock()


if __name__ == "__main__":
    main()
