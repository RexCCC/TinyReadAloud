"""Startup health checks, structured logging, and retry helpers."""

from __future__ import annotations

import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class HealthReport:
    ok: bool
    checks: list[CheckResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        failed = [c for c in self.checks if not c.ok]
        if self.ok:
            return "All systems ready"
        parts = [f"{c.name}: {c.detail}" for c in failed]
        return "; ".join(parts)


def log_event(component: str, message: str, **fields):
    """Structured single-line log for grep-friendly diagnostics."""
    extra = " ".join(f"{k}={v!r}" for k, v in fields.items())
    line = f"[{component}] {message}"
    if extra:
        line = f"{line} {extra}"
    print(line, flush=True)


def retry(
    fn: Callable[[], Any],
    *,
    attempts: int = 3,
    delay_sec: float = 0.15,
    backoff: float = 2.0,
    component: str = "retry",
) -> Any:
    """Retry flaky operations (OCR, screenshot) with exponential backoff."""
    last_exc = None
    wait = delay_sec
    for attempt in range(1, attempts + 1):
        try:
            result = fn()
            if result:
                return result
        except Exception as exc:
            last_exc = exc
            log_event(component, "attempt failed", attempt=attempt, error=str(exc))
        if attempt < attempts:
            time.sleep(wait)
            wait *= backoff
    if last_exc:
        raise last_exc
    return None


def check_ocr_engine() -> CheckResult:
    if sys.platform != "win32":
        return CheckResult("ocr_engine", False, "Windows only")
    try:
        from winrt.windows.media.ocr import OcrEngine
    except ImportError as exc:
        return CheckResult("ocr_engine", False, f"winrt missing: {exc}")
    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        try:
            from winrt.windows.globalization import Language
            engine = OcrEngine.try_create_from_language(Language("en-US"))
        except Exception as exc:
            return CheckResult("ocr_engine", False, f"no engine: {exc}")
    if engine is None:
        return CheckResult(
            "ocr_engine", False,
            "Install English OCR: Settings → Time & language → Language",
        )
    return CheckResult("ocr_engine", True, "ready")


def check_mss() -> CheckResult:
    try:
        import mss
        with mss.MSS() as sct:
            if len(sct.monitors) < 2:
                return CheckResult("mss", False, "no monitors detected")
            v = sct.monitors[0]
            detail = f"virtual {v['width']}x{v['height']} @ ({v['left']},{v['top']})"
        return CheckResult("mss", True, detail)
    except Exception as exc:
        return CheckResult("mss", False, str(exc))


def check_uiautomation() -> CheckResult:
    try:
        import uiautomation  # noqa: F401
        return CheckResult("uiautomation", True, "ready")
    except Exception as exc:
        return CheckResult("uiautomation", False, str(exc))


def check_clipboard() -> CheckResult:
    try:
        import app as app_mod
        ok = app_mod.clipboard_set_text("__tra_health__")
        if not ok:
            return CheckResult("clipboard", False, "set failed")
        text = app_mod.clipboard_get_text()
        if text != "__tra_health__":
            return CheckResult("clipboard", False, "read mismatch")
        return CheckResult("clipboard", True, "ready")
    except Exception as exc:
        return CheckResult("clipboard", False, str(exc))


def run_health_check(include_clipboard: bool = False) -> HealthReport:
    """Preflight all capture/read subsystems."""
    checks = [
        check_ocr_engine(),
        check_mss(),
        check_uiautomation(),
    ]
    if include_clipboard:
        checks.append(check_clipboard())

    warnings = []
    critical = {"ocr_engine", "mss"}
    ok = all(c.ok for c in checks if c.name in critical)
    for c in checks:
        if not c.ok and c.name not in critical:
            warnings.append(f"{c.name}: {c.detail}")

    report = HealthReport(ok=ok, checks=checks, warnings=warnings)
    log_event("Health", report.summary(), ok=report.ok)
    for c in checks:
        log_event("Health", c.name, ok=c.ok, detail=c.detail)
    return report


def new_trace_id() -> str:
    return uuid.uuid4().hex[:8]


def log_exception(component: str, exc: BaseException, trace_id: str = ""):
    log_event(component, "exception", trace_id=trace_id or "-", error=str(exc))
    traceback.print_exc()
