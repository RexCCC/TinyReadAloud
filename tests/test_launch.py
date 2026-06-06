"""Launch smoke test — verifies TinyReadAloud starts without crashing."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PYTHON = os.path.join(ROOT, "venv", "Scripts", "python.exe")
RUN_PY = os.path.join(ROOT, "run.py")
LOG_PATH = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~\\AppData\\Local")),
    "TinyReadAloud",
    "tinyreadaloud.log",
)
LOCK_PATH = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~\\AppData\\Local")),
    "TinyReadAloud",
    ".instance.lock",
)
READY_MARKERS = ("TinyReadAloud v", "ready.")
FAIL_MARKERS = (
    "ImportError",
    "DLL load failed",
    "failed to start",
    "Traceback (most recent call last)",
)


def _log_tail(since_pos: int = 0) -> tuple[str, int]:
    if not os.path.isfile(LOG_PATH):
        return "", since_pos
    with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
        f.seek(since_pos)
        text = f.read()
        return text, f.tell()


def _stop_existing():
    """Best-effort stop of TinyReadAloud run.py processes."""
    try:
        ps = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.CommandLine -match 'TinyReadAloud' -and $_.CommandLine -match 'run.py' } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass
    if os.path.isfile(LOCK_PATH):
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass
    time.sleep(0.5)


class TestLaunchSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(PYTHON):
            raise unittest.SkipTest(f"venv missing: {PYTHON}")
        if not os.path.isfile(RUN_PY):
            raise unittest.SkipTest(f"run.py missing: {RUN_PY}")

    def setUp(self):
        _stop_existing()
        self._proc = None
        self._log_pos = os.path.getsize(LOG_PATH) if os.path.isfile(LOG_PATH) else 0

    def tearDown(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        _stop_existing()

    def _wait_for_ready(self, timeout_sec=45) -> str:
        deadline = time.monotonic() + timeout_sec
        accumulated = ""
        while time.monotonic() < deadline:
            if self._proc and self._proc.poll() is not None:
                chunk, self._log_pos = _log_tail(self._log_pos)
                accumulated += chunk
                self.fail(
                    f"Process exited early (code {self._proc.returncode}).\n"
                    f"Log tail:\n{accumulated[-3000:]}"
                )
            chunk, self._log_pos = _log_tail(self._log_pos)
            accumulated += chunk
            for fail in FAIL_MARKERS:
                if fail in accumulated and "ready." not in accumulated:
                    self.fail(f"Startup failure in log ({fail}).\n{accumulated[-3000:]}")
            if any(m in accumulated for m in READY_MARKERS):
                if "Loading Kokoro TTS model" in accumulated:
                    return accumulated
            time.sleep(0.5)
        self.fail(f"Timed out waiting for ready message.\nLog tail:\n{accumulated[-3000:]}")

    def test_import_app_main(self):
        """Import chain used by run.py (bootstrap → app → onnxruntime)."""
        import bootstrap
        bootstrap.configure_runtime()
        import onnxruntime as ort
        providers = ort.get_available_providers()
        self.assertIn("CPUExecutionProvider", providers)
        import app  # noqa: F401
        self.assertTrue(callable(app.main))

    def test_launch_run_py(self):
        """Full launch via run.py — same path as launch.vbs."""
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        self._proc = subprocess.Popen(
            [PYTHON, RUN_PY],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        log = self._wait_for_ready(timeout_sec=60)
        self.assertIn("Using GPU", log)
        self.assertTrue(self._proc.poll() is None, "Process died after reaching ready state")


if __name__ == "__main__":
    unittest.main(verbosity=2)
