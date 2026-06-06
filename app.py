"""TinyReadAloud - Select text, press Ctrl+Alt+R, hear it read aloud."""

import asyncio
import ctypes
import ctypes.wintypes
import json
import os
import queue
import re
import signal
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
import urllib.error
import urllib.request

import bootstrap

bootstrap.configure_runtime()

from capture_fallbacks import capture_via_accessibility, capture_ocr_region
from ocr_region import RegionSelectOverlay
from reliability import log_event, log_exception, new_trace_id, run_health_check
from version import __version__

# When frozen as a windowless app (console=False), stdout/stderr are None.
# Redirect to a log file so print() calls don't crash.
if getattr(sys, 'frozen', False) and sys.stdout is None:
    _data_base = os.environ.get("LOCALAPPDATA",
                                os.path.expanduser("~\\AppData\\Local"))
    _log_dir = os.path.join(_data_base, "TinyReadAloud")
    os.makedirs(_log_dir, exist_ok=True)
    sys.stdout = open(os.path.join(_log_dir, "tinyreadaloud.log"), "a", encoding="utf-8")
    sys.stderr = sys.stdout

import keyboard
import numpy as np
import onnxruntime as ort
import pystray
import sounddevice as sd
from kokoro_onnx import Kokoro
from langdetect import detect as langdetect_detect
from PIL import Image, ImageDraw

# ── Constants ────────────────────────────────────────────────────────────────

HOTKEY = "ctrl+alt+r"
DICTATION_HOTKEY = "ctrl+alt+d"
GRAMMAR_HOTKEY = "ctrl+alt+g"
REPHRASE_HOTKEY = "ctrl+alt+p"
OCR_HOTKEY = "ctrl+alt+o"
DEFAULT_VOICE_EN = "af_heart"
DEFAULT_VOICE_ES = "ef_dora"
DEFAULT_SPEED = 1.0
DEFAULT_GRAMMAR_MODE = "manual"
GRAMMAR_MODES = ["off", "manual", "after_dictation"]
DEFAULT_DICTATION_PROVIDER = "windows"
DEFAULT_GRAMMAR_PROVIDER = "anthropic"
DICTATION_PROVIDERS = ["windows"]
GRAMMAR_PROVIDERS = ["anthropic"]
REPHRASE_STYLES = ["Natural", "Formal", "Casual", "Concise", "Expanded", "Professional"]
DEFAULT_REPHRASE_STYLE = "Natural"
DEFAULT_STYLE_HOTKEYS = {s: "" for s in REPHRASE_STYLES}
RECALL_STYLE_HOTKEY = ""
DEFAULT_MIC_DEVICE = ""  # empty = system default
COPY_WAIT_INTERVAL = 0.02
COPY_WAIT_TIMEOUT = 1.5
AUDIO_CHUNK_SECS = 0.05  # 50ms playback granularity for stop responsiveness
SKIP_SECONDS = 10
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL_DEFAULT = "claude-sonnet-4-6"
ANTHROPIC_VERSION = "2023-06-01"


def _get_app_dir():
    """Return the application directory (where the exe/script lives)."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _get_data_dir():
    """Return %LOCALAPPDATA%/TinyReadAloud, creating it if needed."""
    base = os.environ.get("LOCALAPPDATA",
                          os.path.expanduser("~\\AppData\\Local"))
    d = os.path.join(base, "TinyReadAloud")
    os.makedirs(d, exist_ok=True)
    return d


APP_DIR = _get_app_dir()
DATA_DIR = _get_data_dir()
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
MODEL_PATH_FP16 = os.path.join(DATA_DIR, "kokoro-v1.0.fp16.onnx")
MODEL_PATH_INT8 = os.path.join(DATA_DIR, "kokoro-v1.0.int8.onnx")
VOICES_PATH = os.path.join(DATA_DIR, "voices-v1.0.bin")
MODEL_URL_FP16 = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.fp16.onnx"
MODEL_URL_INT8 = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx"
VOICES_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
GMEM_ZEROINIT = 0x0040

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.OpenClipboard.argtypes = [ctypes.wintypes.HWND]
user32.OpenClipboard.restype = ctypes.wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = ctypes.wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = ctypes.wintypes.BOOL
user32.GetClipboardData.argtypes = [ctypes.wintypes.UINT]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [ctypes.wintypes.UINT, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.IsClipboardFormatAvailable.argtypes = [ctypes.wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = ctypes.wintypes.BOOL
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
user32.SetForegroundWindow.argtypes = [ctypes.wintypes.HWND]
user32.SetForegroundWindow.restype = ctypes.wintypes.BOOL
user32.BringWindowToTop.argtypes = [ctypes.wintypes.HWND]
user32.BringWindowToTop.restype = ctypes.wintypes.BOOL
user32.AttachThreadInput.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.wintypes.BOOL]
user32.AttachThreadInput.restype = ctypes.wintypes.BOOL
user32.SendMessageW.argtypes = [
    ctypes.wintypes.HWND, ctypes.wintypes.UINT,
    ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM,
]
user32.SendMessageW.restype = ctypes.c_longlong
user32.GetClipboardSequenceNumber.argtypes = []
user32.GetClipboardSequenceNumber.restype = ctypes.wintypes.DWORD
kernel32.GlobalAlloc.argtypes = [ctypes.wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.restype = ctypes.wintypes.BOOL
kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
kernel32.GlobalFree.restype = ctypes.c_void_p

# ── Win32 focus / caret detection ─────────────────────────────────────────────

class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize",        ctypes.wintypes.DWORD),
        ("flags",         ctypes.wintypes.DWORD),
        ("hwndActive",    ctypes.wintypes.HWND),
        ("hwndFocus",     ctypes.wintypes.HWND),
        ("hwndCapture",   ctypes.wintypes.HWND),
        ("hwndMenuOwner", ctypes.wintypes.HWND),
        ("hwndMoveSize",  ctypes.wintypes.HWND),
        ("hwndCaret",     ctypes.wintypes.HWND),
        ("rcCaret",       ctypes.wintypes.RECT),
    ]

user32.GetGUIThreadInfo.argtypes = [ctypes.wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
user32.GetGUIThreadInfo.restype = ctypes.wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD
user32.GetClassNameW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int

_DESKTOP_CLASSES = frozenset({"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"})

def _is_textfield_focused():
    """Return True if the foreground window likely has a text field focused."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    # Check if the foreground window is the desktop / taskbar
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    if buf.value in _DESKTOP_CLASSES:
        return False
    # Get GUI thread info for the foreground window's thread
    tid = user32.GetWindowThreadProcessId(hwnd, None)
    gti = GUITHREADINFO()
    gti.cbSize = ctypes.sizeof(GUITHREADINFO)
    if user32.GetGUIThreadInfo(tid, ctypes.byref(gti)):
        if not gti.hwndFocus:
            return False
    return True

# ── Win32 modifier key helpers ────────────────────────────────────────────────

_VK_CONTROL = 0x11
_VK_MENU    = 0x12   # Alt
_VK_SHIFT   = 0x10
_MODIFIER_VKS = (_VK_CONTROL, _VK_MENU, _VK_SHIFT)

user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype  = ctypes.c_short

def _wait_for_modifiers_released(timeout=2.0):
    """Spin until Ctrl, Alt, and Shift are all physically released.

    With suppress=False hotkeys the physical keys are still held when the
    callback fires.  We must wait for the user to release them before
    injecting keyboard.send('ctrl+a') etc., otherwise the target app
    receives ctrl+alt+a instead of ctrl+a.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in _MODIFIER_VKS):
            return
        time.sleep(0.02)
    # Timed out — proceed anyway (user may be holding the key intentionally)


# ── Model Download ───────────────────────────────────────────────────────────

def _has_cuda():
    """Check if CUDA execution provider is available."""
    try:
        return "CUDAExecutionProvider" in ort.get_available_providers()
    except Exception:
        return False


USE_GPU = _has_cuda()


def ensure_models():
    """Download model files if they don't exist. Returns True if ready."""
    model_path = MODEL_PATH_FP16 if USE_GPU else MODEL_PATH_INT8
    model_url = MODEL_URL_FP16 if USE_GPU else MODEL_URL_INT8
    # Remove the other model variant to save disk space
    other = MODEL_PATH_INT8 if USE_GPU else MODEL_PATH_FP16
    if os.path.exists(other):
        os.remove(other)
    for path, url in [(model_path, model_url), (VOICES_PATH, VOICES_URL)]:
        if os.path.exists(path):
            continue
        name = os.path.basename(path)
        print(f"Downloading {name} (first run only)...")

        def reporthook(block, block_size, total):
            done = block * block_size
            pct = min(100, done * 100 // max(total, 1))
            mb_done = done / 1048576
            mb_total = total / 1048576
            print(f"\r  {pct}%  ({mb_done:.1f} / {mb_total:.1f} MB)", end="", flush=True)

        try:
            urllib.request.urlretrieve(url, path, reporthook=reporthook)
            print()
        except Exception as e:
            print(f"\nDownload failed: {e}", file=sys.stderr)
            if os.path.exists(path):
                os.remove(path)
            return False
    return True


SPEED_PRESETS = (0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.5, 1.8, 2.0)
SPEED_MIN, SPEED_MAX = 0.5, 3.0


def format_speed(value: float) -> str:
    return f"{float(value):.1f}"


def parse_speed(text) -> float:
    try:
        cleaned = str(text).strip().lower().replace("×", "").replace("x", "")
        val = float(cleaned)
    except (TypeError, ValueError):
        return DEFAULT_SPEED
    return max(SPEED_MIN, min(SPEED_MAX, round(val, 2)))


def clamp_speed(value) -> float:
    return max(SPEED_MIN, min(SPEED_MAX, round(float(value), 2)))


def preset_index(value: float) -> int:
    v = clamp_speed(value)
    best_i = 0
    best_d = abs(SPEED_PRESETS[0] - v)
    for i, preset in enumerate(SPEED_PRESETS):
        dist = abs(preset - v)
        if dist < best_d:
            best_d = dist
            best_i = i
    return best_i


# ── Config ──────────────────────────────────────────────────────────────────

def load_config():
    """Load settings from config.json, returning defaults for missing keys."""
    defaults = {"hotkey": HOTKEY, "voice_en": DEFAULT_VOICE_EN,
                "voice_es": DEFAULT_VOICE_ES, "speed": DEFAULT_SPEED,
                "ocr_hotkey": OCR_HOTKEY,
                "dictation_hotkey": DICTATION_HOTKEY,
                "grammar_hotkey": GRAMMAR_HOTKEY,
                "rephrase_hotkey": REPHRASE_HOTKEY,
                "grammar_mode": DEFAULT_GRAMMAR_MODE,
                "dictation_provider": DEFAULT_DICTATION_PROVIDER,
                "grammar_provider": DEFAULT_GRAMMAR_PROVIDER,
                "rephrase_style": DEFAULT_REPHRASE_STYLE,
                "style_hotkeys": dict(DEFAULT_STYLE_HOTKEYS),
                "recall_style_hotkey": RECALL_STYLE_HOTKEY,
                "mic_device": DEFAULT_MIC_DEVICE,
                "anthropic_api_key": "",
                "anthropic_model": ANTHROPIC_MODEL_DEFAULT}
    if not os.path.exists(CONFIG_PATH):
        return defaults
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Migrate old single-voice config
        if "voice" in data and "voice_en" not in data:
            data["voice_en"] = data.pop("voice")
        for k, v in defaults.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return defaults


def save_config(cfg):
    """Save settings dict to config.json."""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def grammar_check_text_anthropic(text, api_key="", model=""):
    """Use Anthropic to correct grammar and return corrected plain text."""
    api_key = (api_key or "").strip() or os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    model = (model or "").strip() or ANTHROPIC_MODEL_DEFAULT

    prompt = (
        "Correct grammar and punctuation for the text below. "
        "Do not add commentary. Return only corrected text in the same language.\n\n"
        f"{text}"
    )
    payload = {
        "model": model,
        "max_tokens": max(256, min(2048, len(text) * 3)),
        "messages": [{"role": "user", "content": prompt}],
    }

    req = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {body or e.reason}") from e

    parts = []
    for item in data.get("content", []):
        if item.get("type") == "text":
            parts.append(item.get("text", ""))
    corrected = "".join(parts).strip()
    if not corrected:
        return text
    return corrected


def grammar_check_text(text, provider, api_key="", model=""):
    """Dispatch grammar check to the selected provider."""
    if provider == "anthropic":
        return grammar_check_text_anthropic(text, api_key=api_key, model=model)
    raise RuntimeError(f"Unsupported grammar provider: {provider}")


_REPHRASE_STYLE_PROMPTS = {
    "Natural":      (
        "Rewrite the text below using different words and sentence structure. "
        "Do NOT just fix grammar or punctuation — actively rephrase with new wording so the output "
        "sounds noticeably different from the input while keeping the same meaning and language."
    ),
    "Formal":       (
        "Rewrite the text below in a formal, professional tone using different vocabulary and sentence structure. "
        "Do NOT just fix grammar — actively rephrase with elevated, polished wording. Keep the same meaning and language."
    ),
    "Casual":       (
        "Rewrite the text below in a relaxed, conversational tone using different words and phrasing. "
        "Do NOT just fix grammar — actively rephrase to sound friendly and informal. Keep the same meaning and language."
    ),
    "Concise":      (
        "Rewrite the text below to be significantly shorter and more direct. "
        "Cut unnecessary words, merge sentences where possible, and use tighter phrasing. "
        "Keep the core meaning and language but make it noticeably more compact."
    ),
    "Expanded":     (
        "Rewrite the text below to be longer and more detailed. "
        "Add descriptive language, elaborate on ideas, and use fuller sentences. "
        "Keep the same meaning and language but make the output noticeably richer and more developed."
    ),
    "Professional": (
        "Rewrite the text below in polished business language suitable for a workplace email or report. "
        "Use different words and sentence structure — do NOT just fix grammar. "
        "Keep the same meaning and language."
    ),
}


def rephrase_text_anthropic(text, api_key="", model="", style=""):
    """Use Anthropic to rephrase/reword text and return the result."""
    api_key = (api_key or "").strip() or os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    model = (model or "").strip() or ANTHROPIC_MODEL_DEFAULT
    style = (style or DEFAULT_REPHRASE_STYLE).strip()
    style_instr = _REPHRASE_STYLE_PROMPTS.get(style, _REPHRASE_STYLE_PROMPTS[DEFAULT_REPHRASE_STYLE])

    system_prompt = (
        "You are a professional writing assistant. "
        "Your sole task is to rewrite the text the user gives you according to the style instruction. "
        "Output ONLY the rewritten text — no quotes, no labels, no commentary, no explanation."
    )
    user_message = f"{style_instr}\n\nText to rewrite:\n{text}"

    payload = {
        "model": model,
        "max_tokens": max(256, min(2048, len(text) * 4)),
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_message}],
    }

    req = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {body or e.reason}") from e

    print(f"[Rephrase] Raw API response type={data.get('type')} stop_reason={data.get('stop_reason')}", flush=True)

    # Anthropic sometimes returns a 200 with {"type": "error", ...}
    if data.get("type") == "error":
        err = data.get("error", {})
        raise RuntimeError(f"{err.get('type', 'api_error')}: {err.get('message', str(data))}")

    parts = []
    for item in data.get("content", []):
        if item.get("type") == "text":
            parts.append(item.get("text", ""))
    result = "".join(parts).strip()
    print(f"[Rephrase] Extracted result ({len(result)} chars): {result[:120]!r}", flush=True)
    if not result:
        raise RuntimeError("API returned empty content — check model name and API key tier.")
    return result


def rephrase_text(text, provider, api_key="", model="", style=""):
    """Dispatch rephrase to the selected provider."""
    if provider == "anthropic":
        return rephrase_text_anthropic(text, api_key=api_key, model=model, style=style)
    raise RuntimeError(f"Unsupported rephrase provider: {provider}")


# ── Clipboard ────────────────────────────────────────────────────────────────

def _open_clipboard(retries=8):
    for i in range(retries):
        if user32.OpenClipboard(0):
            return True
        time.sleep(0.05)
    return False


WM_COPY = 0x0301


def _get_focus_for_window(hwnd):
    """Return the child HWND with keyboard focus for hwnd's thread."""
    if not hwnd:
        return 0
    tid = user32.GetWindowThreadProcessId(hwnd, None)
    gti = GUITHREADINFO()
    gti.cbSize = ctypes.sizeof(GUITHREADINFO)
    if user32.GetGUIThreadInfo(tid, ctypes.byref(gti)) and gti.hwndFocus:
        return gti.hwndFocus
    return hwnd


def _focus_window(hwnd):
    """Bring hwnd to foreground (works better with Electron apps like Cursor)."""
    if not hwnd:
        return False
    fg = user32.GetForegroundWindow()
    if fg == hwnd:
        return True
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    attached = False
    try:
        if fg_thread and target_thread and fg_thread != target_thread:
            user32.AttachThreadInput(fg_thread, target_thread, True)
            attached = True
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(fg_thread, target_thread, False)
    time.sleep(0.15)
    return True


def clipboard_get_text():
    if not _open_clipboard():
        return ""
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return ""
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr) or ""
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def clipboard_set_text(text):
    """Write text to clipboard, retrying up to 10 times. Returns True on success."""
    for attempt in range(10):
        if _open_clipboard():
            try:
                user32.EmptyClipboard()
                if not text:
                    return True
                buf = (text + "\0").encode("utf-16-le")
                hmem = kernel32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, len(buf))
                if not hmem:
                    continue
                ptr = kernel32.GlobalLock(hmem)
                if not ptr:
                    kernel32.GlobalFree(hmem)
                    continue
                ctypes.memmove(ptr, buf, len(buf))
                kernel32.GlobalUnlock(hmem)
                user32.SetClipboardData(CF_UNICODETEXT, hmem)
                return True
            finally:
                user32.CloseClipboard()
        time.sleep(0.05)
    return False


def clipboard_clear():
    if not _open_clipboard():
        return False
    try:
        user32.EmptyClipboard()
        return True
    finally:
        user32.CloseClipboard()


def _clipboard_seq():
    try:
        return user32.GetClipboardSequenceNumber()
    except Exception:
        return 0


def _try_wm_copy(hwnd):
    if not hwnd:
        return False
    try:
        user32.SendMessageW(hwnd, WM_COPY, 0, 0)
        return True
    except Exception:
        return False


def capture_selected_text(target_hwnd=None):
    """Copy the current selection from target_hwnd into a string."""
    _wait_for_modifiers_released()
    old = clipboard_get_text()

    for attempt in range(4):
        if target_hwnd:
            _focus_window(target_hwnd)
            time.sleep(0.12 + attempt * 0.05)

        for _ in range(8):
            if clipboard_clear():
                break
            time.sleep(0.04)
        time.sleep(0.06)

        focus_hwnd = _get_focus_for_window(
            target_hwnd or user32.GetForegroundWindow()
        )
        copy_targets = []
        for hwnd in (focus_hwnd, target_hwnd):
            if hwnd and hwnd not in copy_targets:
                copy_targets.append(hwnd)

        copied = False
        for hwnd in copy_targets:
            seq_before = _clipboard_seq()
            _try_wm_copy(hwnd)
            time.sleep(0.08)
            if _clipboard_seq() != seq_before:
                copied = True
                break

        if not copied:
            seq_before = _clipboard_seq()
            keyboard.send("ctrl+c")
            deadline = time.monotonic() + COPY_WAIT_TIMEOUT
            while time.monotonic() < deadline:
                time.sleep(COPY_WAIT_INTERVAL)
                if _clipboard_seq() != seq_before:
                    copied = True
                    break

        text = clipboard_get_text().strip() if copied else ""
        if text:
            print(f"[Read] capture ok attempt={attempt + 1} len={len(text)}", flush=True)
            return text

        print(f"[Read] capture attempt {attempt + 1} failed", flush=True)
        time.sleep(0.1)

    if old:
        clipboard_set_text(old)
    print("[Read] capture failed after retries", flush=True)
    return ""


def split_sentences(text):
    """Split text into sentence chunks for skip navigation."""
    text = (text or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?…])\s+", text)
    sentences = [p.strip() for p in parts if p.strip()]
    return sentences if sentences else [text]


# ── Icon ─────────────────────────────────────────────────────────────────────

def create_tray_icon(size=64, speaking=False):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    bg = "#2ECC71" if speaking else "#E74C3C"
    pad = 2
    draw.ellipse([pad, pad, size - pad, size - pad], fill=bg)

    cx, cy = size // 2, size // 2

    # Speaker body
    draw.rectangle([cx - 12, cy - 6, cx - 4, cy + 6], fill="white")
    # Speaker cone
    draw.polygon(
        [(cx - 4, cy - 6), (cx + 6, cy - 14), (cx + 6, cy + 14), (cx - 4, cy + 6)],
        fill="white",
    )

    if speaking:
        draw.rectangle([cx + 12, cy - 7, cx + 17, cy + 7], fill="white")
        draw.rectangle([cx + 20, cy - 7, cx + 25, cy + 7], fill="white")
    else:
        for r in [14, 21]:
            bbox = [cx + 6 - r, cy - r, cx + 6 + r, cy + r]
            draw.arc(bbox, start=-35, end=35, fill="white", width=2)

    return img


# ── Voice Helpers ────────────────────────────────────────────────────────────

_LANG_NAMES = {"a": "American", "b": "British", "e": "Spanish", "f": "French",
               "h": "Hindi", "i": "Italian", "j": "Japanese", "p": "Portuguese",
               "z": "Chinese"}
_GENDER_NAMES = {"f": "Female", "m": "Male"}


def voice_display_name(code):
    """Convert 'af_heart' to 'Heart (American, Female)'."""
    parts = code.split("_", 1)
    if len(parts) != 2 or len(parts[0]) < 2:
        return code
    prefix, name = parts
    lang = _LANG_NAMES.get(prefix[0], prefix[0].upper())
    gender = _GENDER_NAMES.get(prefix[1], prefix[1].upper())
    return f"{name.title()} ({lang}, {gender})"


def is_english_voice(code):
    """Voice codes starting with 'a' (American) or 'b' (British) are English."""
    return len(code) >= 2 and code[0] in ("a", "b")


def is_spanish_voice(code):
    """Voice codes starting with 'e' are Spanish."""
    return len(code) >= 2 and code[0] == "e"


# Kokoro lang parameter mapping
_KOKORO_LANG = {"en": "en-us", "es": "es"}


def detect_language(text):
    """Detect whether text is English or Spanish. Returns 'en' or 'es'."""
    try:
        lang = langdetect_detect(text)
        if lang.startswith("es"):
            return "es"
    except Exception:
        pass
    return "en"


# ── Floating Status Bar ──────────────────────────────────────────────────────

_STATUSBAR_TRANSPARENT = "#010203"


class FloatingStatusBar:
    """Tiny draggable always-on-top bar: style switcher + live status + settings gear."""

    # Google Gemini dark theme (blue → purple aurora accent)
    BG         = "#1E1F20"
    BG_ELEV    = "#28292A"
    FG         = "#E8EAED"
    DIM        = "#9AA0A6"
    ACCENT     = "#4796E3"   # Gemini blue
    ACCENT2    = "#9177C7"   # Gemini purple
    GEM_ROSE   = "#CA6673"
    SEP        = "#3C4043"
    BORDER     = "#5F6368"
    PILL_READ  = "#1A3A5C"
    PILL_READ_H = "#2563A8"
    PILL_OCR   = "#2D2640"
    PILL_OCR_H = "#4A3D6E"
    CHIP_BG    = "#303134"
    CHIP_HOVER = "#3C4043"
    W, H       = 660, 42
    RADIUS     = 14
    MIN_W      = 640
    _FONT      = ("Segoe UI", 9)
    _FONTB     = ("Segoe UI Semibold", 9, "bold")
    _FONT_ICON = ("Segoe UI Symbol", 10)

    _instance_lock = threading.Lock()
    _instance = None

    # ── Singleton ────────────────────────────────────────────────────────────

    @classmethod
    def open(cls, app):
        with cls._instance_lock:
            if cls._instance is not None:
                try:
                    cls._instance._root.lift()
                    return
                except Exception:
                    pass
            inst = cls(app)
            cls._instance = inst
            threading.Thread(target=inst._run, daemon=True).start()

    @classmethod
    def get(cls):
        with cls._instance_lock:
            return cls._instance

    # ── Init ─────────────────────────────────────────────────────────────────

    def __init__(self, app):
        self._app = app
        self._root = None
        self._status_var = None
        self._style_var = None
        self._drag_x = self._drag_y = 0
        self._own_hwnd = 0          # set once the Tk window is created
        self._last_target_hwnd = 0  # last foreground HWND that isn't the status bar
        self._btn_read = self._btn_pause = self._btn_resume = self._btn_stop = None
        self._btn_prev = self._btn_next = self._btn_back = self._btn_fwd = None
        self._speed_display_var = None
        self._btn_spd_down = self._btn_spd_up = None
        self._btn_ocr = None
        self._pill_read = self._pill_ocr = None
        self._canvas = None
        self._context_menu = None
        try:
            self._style_idx = REPHRASE_STYLES.index(app._rephrase_style)
        except (ValueError, AttributeError):
            self._style_idx = 0

    # ── Thread-safe updates ───────────────────────────────────────────────────

    def set_status(self, text):
        if self._root and self._status_var:
            try:
                self._root.after(0, lambda t=text: self._status_var.set(t))
            except Exception:
                pass

    def sync_style(self):
        """Called after Settings saves a new rephrase style."""
        style = getattr(self._app, "_rephrase_style", REPHRASE_STYLES[0])
        try:
            self._style_idx = REPHRASE_STYLES.index(style)
        except ValueError:
            pass
        if self._root and self._style_var:
            try:
                self._root.after(0, lambda s=style: self._style_var.set(s))
            except Exception:
                pass

    def sync_speed(self, speed=None):
        """Keep toolbar speed display in sync with TTS speed."""
        speed = speed if speed is not None else self._app.tts.current_speed
        label = format_speed(speed)
        if self._root and self._speed_display_var:
            try:
                self._root.after(0, lambda l=label: self._speed_display_var.set(l))
            except Exception:
                pass

    def update_playback_controls(self):
        """Refresh playback button states (thread-safe)."""
        if self._root:
            try:
                self._root.after(0, self._refresh_playback_ui)
            except Exception:
                pass

    def _parse_geometry_pos(self, geo: str) -> str:
        """Return +x+y from a Tk geometry string; default centered top."""
        if geo and "+" in geo:
            i = geo.index("+")
            return geo[i:]
        if self._root:
            sw = self._root.winfo_screenwidth()
            return f"+{(sw - self.W) // 2}+48"
        return "+0+48"

    def prepare_for_ocr_overlay(self):
        """Hide toolbar visually without withdraw (avoids shrink on restore)."""
        if not self._root:
            return
        try:
            self._saved_pos = self._parse_geometry_pos(self._root.geometry())
            self._root.attributes("-alpha", 0.0)
        except Exception:
            pass

    def restore_after_ocr_overlay(self):
        """Restore toolbar size/position after OCR region pick."""
        if not self._root:
            return
        try:
            pos = getattr(self, "_saved_pos", None) or self._parse_geometry_pos("")
            self._root.geometry(f"{self.W}x{self.H}{pos}")
            self._root.update_idletasks()
            self._root.attributes("-alpha", 0.95)
            self._root.lift()
        except Exception:
            pass

    def _refresh_playback_ui(self):
        if not self._root or self._btn_read is None:
            return
        tts = self._app.tts
        active = tts.is_active
        paused = tts.is_paused
        self._btn_read.config(
            state="normal",
            fg=self.FG,
            text="Stop" if active else "✦ Read",
        )
        if self._pill_read:
            self._pill_read.config(bg=self.PILL_READ_H if active else self.PILL_READ)
            self._btn_read.config(bg=self.PILL_READ_H if active else self.PILL_READ)
        for btn, enabled in (
            (self._btn_pause, active and not paused),
            (self._btn_resume, paused),
            (self._btn_stop, active),
            (self._btn_prev, active),
            (self._btn_next, active),
            (self._btn_back, active),
            (self._btn_fwd, active),
        ):
            btn.config(
                state="normal" if enabled else "disabled",
                fg=self.ACCENT if enabled else self.DIM,
            )

    # ── Playback toolbar actions ─────────────────────────────────────────────

    def _tb_read(self, _=None):
        """Read selected text from the last editor window (same as Ctrl+Alt+R)."""
        target = self._last_target_hwnd or user32.GetForegroundWindow()
        self._app.start_read(target)

    def _tb_pause(self, _=None):
        self._app.tts.pause()

    def _tb_resume(self, _=None):
        self._app.tts.resume()

    def _tb_stop(self, _=None):
        self._app.tts.stop()

    def _tb_prev_sentence(self, _=None):
        self._app.tts.prev_sentence()

    def _tb_next_sentence(self, _=None):
        self._app.tts.next_sentence()

    def _tb_skip_back(self, _=None):
        self._app.tts.skip_seconds(-SKIP_SECONDS)

    def _tb_skip_forward(self, _=None):
        self._app.tts.skip_seconds(SKIP_SECONDS)

    def _tb_ocr(self, _=None):
        self._app.start_ocr_read()

    def _step_speed(self, delta, _=None):
        idx = preset_index(self._app.tts.current_speed)
        idx = max(0, min(len(SPEED_PRESETS) - 1, idx + delta))
        self._app.set_reading_speed(SPEED_PRESETS[idx])

    def _build_context_menu(self):
        menu = tk.Menu(
            self._root, tearoff=0,
            bg=self.BG, fg=self.FG,
            activebackground=self.ACCENT, activeforeground=self.BG,
            borderwidth=0,
        )
        menu.add_command(label="Read", command=self._tb_read)
        menu.add_command(label="Read Region (OCR)…", command=self._tb_ocr)
        menu.add_separator()
        menu.add_command(label="Pause", command=self._tb_pause)
        menu.add_command(label="Resume", command=self._tb_resume)
        menu.add_command(label="Stop", command=self._tb_stop)
        menu.add_separator()
        menu.add_command(label="Previous Sentence", command=self._tb_prev_sentence)
        menu.add_command(label="Next Sentence", command=self._tb_next_sentence)
        menu.add_command(label=f"Skip -{SKIP_SECONDS}s", command=self._tb_skip_back)
        menu.add_command(label=f"Skip +{SKIP_SECONDS}s", command=self._tb_skip_forward)
        menu.add_separator()

        speed_menu = tk.Menu(
            menu, tearoff=0,
            bg=self.BG, fg=self.FG,
            activebackground=self.ACCENT, activeforeground=self.BG,
            borderwidth=0,
        )
        for preset in SPEED_PRESETS:
            label = format_speed(preset)
            speed_menu.add_command(
                label=label,
                command=lambda s=preset, l=label: self._set_speed_from_menu(s, l),
            )
        menu.add_cascade(label="Speed", menu=speed_menu)
        menu.add_separator()
        menu.add_command(label="Settings…", command=self._open_settings)
        self._context_menu = menu
        return menu

    def _set_speed_from_menu(self, speed, label):
        if self._speed_display_var:
            self._speed_display_var.set(label)
        self._app.set_reading_speed(speed)

    def _show_context_menu(self, event):
        if self._context_menu is None:
            self._build_context_menu()
        try:
            self._context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                self._context_menu.grab_release()
            except Exception:
                pass

    def _bind_context_menu(self, widget):
        widget.bind("<Button-3>", self._show_context_menu, add="+")

    def _bind_btn(self, btn, command, hover=True, default_fg=None, default_bg=None,
                  hover_fg=None, hover_bg=None):
        default_fg = default_fg if default_fg is not None else btn.cget("fg")
        default_bg = default_bg if default_bg is not None else btn.cget("bg")
        hover_fg = hover_fg if hover_fg is not None else self.ACCENT
        hover_bg = hover_bg if hover_bg is not None else default_bg
        btn.bind("<Button-1>", command)
        if hover:
            def _on_enter(_e):
                if btn.cget("state") == "normal":
                    btn.config(fg=hover_fg, bg=hover_bg)
            def _on_leave(_e):
                if btn.cget("state") == "normal":
                    btn.config(fg=default_fg, bg=default_bg)
                else:
                    btn.config(fg=self.DIM, bg=default_bg)
            btn.bind("<Enter>", _on_enter)
            btn.bind("<Leave>", _on_leave)

    def _bind_pill(self, wrap, lbl, command, default_bg, hover_bg,
                   default_fg=None, hover_fg=None, trigger="<Button-1>"):
        default_fg = default_fg or lbl.cget("fg")
        hover_fg = hover_fg or self.FG
        for w in (wrap, lbl):
            w.bind(trigger, command)
            w.bind("<Enter>", lambda _e: (
                wrap.config(bg=hover_bg), lbl.config(bg=hover_bg, fg=hover_fg)
            ))
            w.bind("<Leave>", lambda _e: (
                wrap.config(bg=default_bg), lbl.config(bg=default_bg, fg=default_fg)
            ))

    @staticmethod
    def _lerp_hex(c1: str, c2: str, t: float) -> str:
        def _rgb(h):
            h = h.lstrip("#")
            return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        r1, g1, b1 = _rgb(c1)
        r2, g2, b2 = _rgb(c2)
        r = int(r1 + (r2 - r1) * t)
        g = int(g1 + (g2 - g1) * t)
        b = int(b1 + (b2 - b1) * t)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _draw_gemini_shell(self, canvas, w, h, r):
        """Dark surface + subtle border + blue→purple aurora strip."""
        canvas.delete("shell")
        self._rounded_rect(
            canvas, 0, 0, w, h, r,
            fill=self.BG, outline=self.BORDER, width=1, tags="shell",
        )
        x0, x1 = r, max(r + 1, w - r)
        span = max(x1 - x0, 1)
        for i in range(span):
            t = i / max(span - 1, 1)
            if t <= 0.55:
                color = self._lerp_hex(self.ACCENT, self.ACCENT2, t / 0.55)
            else:
                color = self._lerp_hex(self.ACCENT2, self.GEM_ROSE, (t - 0.55) / 0.45)
            canvas.create_line(x0 + i, 2, x0 + i, 5, fill=color, tags="shell")

    def _place_btn(self, canvas, btn, x, ymid):
        canvas.create_window(x, ymid, window=btn, anchor="w")
        try:
            btn.configure(takefocus=0)
        except Exception:
            pass
        return btn

    # ── Canvas helper ─────────────────────────────────────────────────────────

    @staticmethod
    def _rounded_rect(canvas, x1, y1, x2, y2, r, **kw):
        pts = [
            x1 + r, y1,   x2 - r, y1,
            x2,     y1,   x2,     y1 + r,
            x2,     y2 - r, x2,   y2,
            x2 - r, y2,   x1 + r, y2,
            x1,     y2,   x1,     y2 - r,
            x1,     y1 + r, x1,   y1,
        ]
        canvas.create_polygon(pts, smooth=True, **kw)

    # ── Drag ─────────────────────────────────────────────────────────────────

    def _drag_start(self, event):
        self._drag_x = event.x_root - self._root.winfo_x()
        self._drag_y = event.y_root - self._root.winfo_y()

    def _drag_move(self, event):
        self._root.geometry(
            f"+{event.x_root - self._drag_x}+{event.y_root - self._drag_y}"
        )

    # ── Style cycling ─────────────────────────────────────────────────────────

    def _prev_style(self, _=None):
        self._style_idx = (self._style_idx - 1) % len(REPHRASE_STYLES)
        self._commit_style()

    def _next_style(self, _=None):
        self._style_idx = (self._style_idx + 1) % len(REPHRASE_STYLES)
        self._commit_style()

    def _commit_style(self):
        style = REPHRASE_STYLES[self._style_idx]
        if self._style_var:
            self._style_var.set(style)
        self._app._rephrase_style = style
        try:
            cfg = load_config()
            cfg["rephrase_style"] = style
            save_config(cfg)
        except Exception:
            pass

    # ── Open settings / trigger rephrase ─────────────────────────────────────

    def _open_settings(self, _=None):
        SettingsWindow.open(self._app)

    def _trigger_rephrase(self, _=None):
        """Triggered when the user clicks the style name — rephrases with current style."""
        # Use the last tracked editor window, NOT GetForegroundWindow() which at
        # click time returns the status bar's own HWND.
        target_hwnd = self._last_target_hwnd or None
        style = self._app._rephrase_style
        target_hex = f"{target_hwnd:#010x}" if target_hwnd else "0"
        print(f"[Rephrase] Status bar click. last_target_hwnd={target_hex}  style={style!r}", flush=True)
        threading.Thread(
            target=self._app._run_rephrase_select_all,
            args=(target_hwnd,),
            daemon=True,
        ).start()

    # ── Tk main loop ──────────────────────────────────────────────────────────

    def _run(self):
        T = _STATUSBAR_TRANSPARENT
        W, H, R = self.W, self.H, self.RADIUS
        ymid = H // 2

        root = tk.Tk()
        self._root = root
        root.overrideredirect(True)
        root.wm_attributes("-topmost", True)
        root.wm_attributes("-alpha", 0.95)
        root.wm_attributes("-transparentcolor", T)
        root.configure(bg=T)
        root.resizable(False, False)

        sw = root.winfo_screenwidth()
        root.geometry(f"{W}x{H}+{(sw - W) // 2}+48")

        # Win11 rounded corners via DWM (safe no-op on Win10)
        try:
            root.update_idletasks()
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                root.winfo_id(), 33,
                ctypes.byref(ctypes.c_int(2)), ctypes.sizeof(ctypes.c_int),
            )
        except Exception:
            pass

        # Prevent this window from ever stealing keyboard focus
        GWL_EXSTYLE      = -20
        WS_EX_NOACTIVATE = 0x08000000
        WS_EX_TOOLWINDOW = 0x00000080
        SWP_NOMOVE       = 0x0002
        SWP_NOSIZE       = 0x0001
        SWP_NOZORDER     = 0x0004
        SWP_FRAMECHANGED = 0x0020
        hwnd = root.winfo_id()
        self._own_hwnd = hwnd
        try:
            cur = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE,
                cur | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
            )
            # Force the extended-style change to take effect immediately
            ctypes.windll.user32.SetWindowPos(
                hwnd, 0, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED,
            )
        except Exception:
            pass

        # Background thread: track the last non-statusbar foreground window
        def _fg_tracker():
            while self._root is not None:
                try:
                    fg = user32.GetForegroundWindow()
                    if fg and fg != self._own_hwnd:
                        self._last_target_hwnd = fg
                except Exception:
                    pass
                time.sleep(0.15)
        threading.Thread(target=_fg_tracker, daemon=True).start()

        # ── Canvas background + Frame toolbar (Gemini dark shell) ──
        canvas = tk.Canvas(root, width=W, height=H, bg=T, highlightthickness=0)
        canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self._canvas = canvas
        self._draw_gemini_shell(canvas, W, H, R)

        bar = tk.Frame(root, bg=self.BG, bd=0, highlightthickness=0)
        bar.place(x=0, y=0, relwidth=1, relheight=1)

        def _sep(parent):
            tk.Frame(parent, bg=self.SEP, width=1, height=H - 18).pack(
                side=tk.LEFT, padx=4, pady=9,
            )

        def _lbl(text="", textvariable=None, font=None, fg=None, bg=None,
                 width=None, anchor="center", cursor="arrow", parent=bar):
            kw = dict(bg=bg or self.BG, fg=fg or self.FG,
                      font=font or self._FONT, cursor=cursor, bd=0)
            if textvariable is not None:
                kw["textvariable"] = textvariable
            else:
                kw["text"] = text
            if width:
                kw["width"] = width
            if anchor:
                kw["anchor"] = anchor
            return tk.Label(parent, **kw)

        def _pill(parent, text, pill_bg, pill_hover, fg, cmd, bold=False, padx=10,
                  trigger_on_release=False):
            wrap = tk.Frame(parent, bg=pill_bg, bd=0, highlightthickness=0)
            lbl = tk.Label(
                wrap, text=text, bg=pill_bg, fg=fg,
                font=self._FONTB if bold else self._FONT,
                cursor="hand2", bd=0, padx=padx, pady=4,
            )
            lbl.pack()
            trigger = "<ButtonRelease-1>" if trigger_on_release else "<Button-1>"
            self._bind_pill(wrap, lbl, cmd, pill_bg, pill_hover, default_fg=fg, trigger=trigger)
            wrap.pack(side=tk.LEFT, padx=(0, 4))
            return wrap, lbl

        left = tk.Frame(bar, bg=self.BG, bd=0)
        left.pack(side=tk.LEFT, padx=(10, 0), pady=5)

        self._pill_read, self._btn_read = _pill(
            left, "✦ Read", self.PILL_READ, self.PILL_READ_H, self.FG, self._tb_read, bold=True,
        )
        _sep(left)

        for sym, cmd, w, attr in (
            ("⏸", self._tb_pause, 2, "_btn_pause"),
            ("▶", self._tb_resume, 2, "_btn_resume"),
            ("⏹", self._tb_stop, 2, "_btn_stop"),
        ):
            btn = _lbl(sym, fg=self.DIM, font=self._FONT_ICON, cursor="hand2",
                       width=w, parent=left)
            self._bind_btn(btn, cmd, default_fg=self.DIM, hover_fg=self.ACCENT)
            btn.pack(side=tk.LEFT, padx=1)
            setattr(self, attr, btn)
        _sep(left)

        self._btn_prev = _lbl("⏮", fg=self.DIM, font=self._FONT_ICON, cursor="hand2",
                              width=2, parent=left)
        self._bind_btn(self._btn_prev, self._tb_prev_sentence,
                       default_fg=self.DIM, hover_fg=self.ACCENT)
        self._btn_prev.pack(side=tk.LEFT, padx=1)
        self._btn_next = _lbl("⏭", fg=self.DIM, font=self._FONT_ICON, cursor="hand2",
                              width=2, parent=left)
        self._bind_btn(self._btn_next, self._tb_next_sentence,
                       default_fg=self.DIM, hover_fg=self.ACCENT)
        self._btn_next.pack(side=tk.LEFT, padx=1)
        _sep(left)

        self._btn_back = _lbl(f"−{SKIP_SECONDS}s", fg=self.DIM, cursor="hand2",
                              width=4, parent=left)
        self._bind_btn(self._btn_back, self._tb_skip_back,
                       default_fg=self.DIM, hover_fg=self.ACCENT2)
        self._btn_back.pack(side=tk.LEFT, padx=1)
        self._btn_fwd = _lbl(f"+{SKIP_SECONDS}s", fg=self.DIM, cursor="hand2",
                             width=4, parent=left)
        self._bind_btn(self._btn_fwd, self._tb_skip_forward,
                       default_fg=self.DIM, hover_fg=self.ACCENT2)
        self._btn_fwd.pack(side=tk.LEFT, padx=(1, 2))
        _sep(left)

        self._pill_ocr, self._btn_ocr = _pill(
            left, "OCR", self.PILL_OCR, self.PILL_OCR_H, self.ACCENT2,
            self._tb_ocr, bold=True, padx=8, trigger_on_release=True,
        )

        # Status — flexible center
        self._status_var = tk.StringVar(value="Ready")
        lbl_status = _lbl(textvariable=self._status_var, fg=self.DIM, width=14,
                          anchor="w", parent=bar)
        lbl_status.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 4))

        # Right cluster: speed chip + style + gear
        right = tk.Frame(bar, bg=self.BG, bd=0)
        right.pack(side=tk.RIGHT, padx=(0, 10), pady=5)

        btn_gear = _lbl("⚙", font=("Segoe UI", 11), fg=self.DIM, cursor="hand2", parent=right)
        self._bind_btn(btn_gear, self._open_settings, default_fg=self.DIM, hover_fg=self.ACCENT)
        btn_gear.pack(side=tk.RIGHT, padx=(6, 0))

        self._style_var = tk.StringVar(value=REPHRASE_STYLES[self._style_idx])
        lbl_style = _lbl(textvariable=self._style_var, font=self._FONTB, fg=self.ACCENT2,
                         width=8, anchor="e", cursor="hand2", parent=right)
        lbl_style.bind("<Button-1>", self._trigger_rephrase)
        lbl_style.bind("<Enter>", lambda e: lbl_style.config(fg=self.FG))
        lbl_style.bind("<Leave>", lambda e: lbl_style.config(fg=self.ACCENT2))
        lbl_style.pack(side=tk.RIGHT, padx=(4, 8))

        _sep(right)

        speed_box = tk.Frame(right, bg=self.CHIP_BG, bd=0, highlightthickness=0)
        speed_box.pack(side=tk.RIGHT, padx=(0, 2))
        speed_inner = tk.Frame(speed_box, bg=self.CHIP_BG, bd=0)
        speed_inner.pack(padx=4, pady=2)
        self._speed_display_var = tk.StringVar(
            value=format_speed(self._app.tts.current_speed)
        )
        self._btn_spd_up = _lbl("+", fg=self.DIM, cursor="hand2", width=2,
                                bg=self.CHIP_BG, parent=speed_inner)
        self._bind_btn(self._btn_spd_up, lambda e: self._step_speed(1),
                        default_fg=self.DIM, default_bg=self.CHIP_BG,
                        hover_fg=self.FG, hover_bg=self.CHIP_HOVER)
        self._btn_spd_up.pack(side=tk.RIGHT)
        lbl_speed = _lbl(textvariable=self._speed_display_var, width=3,
                         anchor="center", fg=self.FG, bg=self.CHIP_BG, parent=speed_inner)
        lbl_speed.pack(side=tk.RIGHT, padx=2)
        self._btn_spd_down = _lbl("−", fg=self.DIM, cursor="hand2", width=2,
                                   bg=self.CHIP_BG, parent=speed_inner)
        self._bind_btn(self._btn_spd_down, lambda e: self._step_speed(-1),
                        default_fg=self.DIM, default_bg=self.CHIP_BG,
                        hover_fg=self.FG, hover_bg=self.CHIP_HOVER)
        self._btn_spd_down.pack(side=tk.RIGHT)

        for w in (bar, left, right, speed_box, speed_inner, lbl_status, lbl_style, btn_gear,
                  lbl_speed, self._btn_read, self._btn_ocr, self._pill_read, self._pill_ocr):
            try:
                w.configure(takefocus=0)
            except Exception:
                pass

        root.update_idletasks()
        req_w = max(bar.winfo_reqwidth() + 12, self.MIN_W)
        if req_w != W:
            W = req_w
            self.W = W
            pos = f"+{(sw - W) // 2}+48"
            root.geometry(f"{W}x{H}{pos}")
            canvas.config(width=W, height=H)
            self._draw_gemini_shell(canvas, W, H, R)

        root.attributes("-topmost", True)

        # drag on background + status label
        for w in (canvas, lbl_status):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

        self._build_context_menu()
        drag_widgets = (
            canvas, lbl_status, lbl_style, btn_gear,
            self._btn_read, self._btn_pause, self._btn_resume, self._btn_stop,
            self._btn_prev, self._btn_next, self._btn_back, self._btn_fwd,
            self._btn_ocr, self._btn_spd_down, self._btn_spd_up, lbl_speed,
            self._pill_read, self._pill_ocr, speed_box, speed_inner,
        )
        for w in drag_widgets:
            self._bind_context_menu(w)

        self._refresh_playback_ui()
        root.protocol("WM_DELETE_WINDOW", lambda: None)
        root.mainloop()


# ── Settings Window ─────────────────────────────────────────────────────────

class SettingsWindow:
    """Tkinter settings dialog — modern themed, grouped into sections."""

    _instance_lock = threading.Lock()
    _instance = None

    # Dark theme colours
    BG      = "#1e1e2e"
    BG2     = "#282840"
    FG      = "#cdd6f4"
    DIM     = "#6c7086"
    ACCENT  = "#a78bfa"
    BORDER  = "#45475a"
    ENTRY_BG = "#313244"
    BTN_BG  = "#585b70"
    BTN_FG  = "#cdd6f4"
    SAVE_BG = "#a78bfa"
    SAVE_FG = "#1e1e2e"

    @classmethod
    def open(cls, app):
        """Open the settings window, or focus it if already open."""
        with cls._instance_lock:
            if cls._instance is not None:
                try:
                    cls._instance._root.after(0, cls._instance._root.lift)
                    return
                except Exception:
                    cls._instance = None
            win = cls(app)
            cls._instance = win
        sb = FloatingStatusBar.get()
        if sb and sb._root:
            sb._root.after(0, win._run, sb._root)
        else:
            threading.Thread(target=win._run, daemon=True).start()

    def __init__(self, app):
        self._app = app
        self._root = None
        self._hotkey_var = None
        self._dictation_hotkey_var = None
        self._grammar_hotkey_var = None
        self._grammar_mode_var = None
        self._dictation_provider_var = None
        self._grammar_provider_var = None
        self._anthropic_api_key_var = None
        self._anthropic_model_var = None
        self._rephrase_style_var = None
        self._recall_style_hotkey_var = None
        self._voice_en_var = None
        self._voice_es_var = None
        self._speed_var = None
        self._mic_device_var = None
        self._recording = False
        self._record_target = None
        self._record_buttons = []  # all record buttons for disable/enable
        self._en_codes = []
        self._en_labels = []
        self._es_codes = []
        self._es_labels = []
        self._style_hotkey_vars = {}

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _make_section(self, parent, title):
        """Create a labelled section frame packed into parent. Returns body frame."""
        wrapper = tk.Frame(parent, bg=self.BG)
        wrapper.pack(fill="x", padx=8, pady=(6, 2))
        # Section header
        hdr = tk.Frame(wrapper, bg=self.BG)
        hdr.pack(fill="x", pady=(0, 2))
        tk.Label(hdr, text=title, font=("Segoe UI Semibold", 10),
                 bg=self.BG, fg=self.ACCENT).pack(side="left")
        sep = tk.Frame(hdr, bg=self.BORDER, height=1)
        sep.pack(side="left", fill="x", expand=True, padx=(8, 0), pady=1)
        # Section body frame
        body = tk.Frame(wrapper, bg=self.BG2, highlightbackground=self.BORDER,
                        highlightthickness=1)
        body.pack(fill="x")
        body.columnconfigure(1, weight=1)
        return body

    def _add_label(self, parent, text, row, col=0, **kw):
        lbl = tk.Label(parent, text=text, font=("Segoe UI", 9),
                       bg=self.BG2, fg=self.FG, anchor="w")
        lbl.grid(row=row, column=col, sticky="w", padx=(10, 4), pady=3, **kw)
        return lbl

    def _add_entry(self, parent, var, row, col=1, width=28, show=None, state="readonly"):
        e = tk.Entry(parent, textvariable=var, width=width, font=("Segoe UI", 9),
                     bg=self.ENTRY_BG, fg=self.FG, insertbackground=self.FG,
                     relief="flat", highlightthickness=1,
                     highlightbackground=self.BORDER, highlightcolor=self.ACCENT)
        if show:
            e.config(show=show)
        if state == "readonly":
            e.config(state="readonly", readonlybackground=self.ENTRY_BG)
        e.grid(row=row, column=col, sticky="ew", padx=4, pady=3)
        return e

    def _add_record_btn(self, parent, command, row, col=2):
        b = tk.Button(parent, text="⏺", font=("Segoe UI", 8), width=3,
                      bg=self.BTN_BG, fg=self.BTN_FG, relief="flat",
                      activebackground=self.ACCENT, activeforeground=self.SAVE_FG,
                      cursor="hand2", command=command)
        b.grid(row=row, column=col, padx=(2, 0), pady=3)
        self._record_buttons.append(b)
        return b

    def _add_clear_btn(self, parent, var, row, col=3):
        b = tk.Button(parent, text="✕", font=("Segoe UI", 8), width=3,
                      bg=self.BTN_BG, fg="#f38ba8", relief="flat",
                      activebackground="#f38ba8", activeforeground=self.SAVE_FG,
                      cursor="hand2", command=lambda: var.set(""))
        b.grid(row=row, column=col, padx=(2, 10), pady=3)
        return b

    def _add_combo(self, parent, var, values, row, col=1, width=26, on_change=None):
        # Use ttk Combobox with readonly, then style it after creation
        cb = ttk.Combobox(parent, textvariable=var, values=values,
                          state="readonly", width=width, font=("Segoe UI", 9))
        cb.grid(row=row, column=col, sticky="ew", padx=4, pady=3, columnspan=3)
        if on_change:
            cb.bind("<<ComboboxSelected>>", on_change)
        return cb

    # ── Build the window ─────────────────────────────────────────────────────

    def _run(self, parent=None):
        if parent:
            root = tk.Toplevel(parent)
        else:
            root = tk.Tk()
        self._root = root
        root.withdraw()
        root.title("TinyReadAloud Settings")
        root.resizable(False, False)
        root.configure(bg=self.BG)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Try to set dark title bar on Windows 10/11
        try:
            root.update_idletasks()
            hwnd = root.winfo_id()
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(ctypes.c_int(1)), ctypes.sizeof(ctypes.c_int))
        except Exception:
            pass

        # Configure ttk style for dark combo boxes
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TCombobox",
                        fieldbackground=self.ENTRY_BG, background=self.BTN_BG,
                        foreground=self.FG, arrowcolor=self.FG,
                        bordercolor=self.BORDER, lightcolor=self.BORDER,
                        darkcolor=self.BORDER, selectbackground=self.ACCENT,
                        selectforeground=self.SAVE_FG)
        style.map("TCombobox",
                  fieldbackground=[("readonly", self.ENTRY_BG)],
                  foreground=[("readonly", self.FG)])
        root.option_add("*TCombobox*Listbox.background", self.ENTRY_BG)
        root.option_add("*TCombobox*Listbox.foreground", self.FG)
        root.option_add("*TCombobox*Listbox.selectBackground", self.ACCENT)
        root.option_add("*TCombobox*Listbox.selectForeground", self.SAVE_FG)

        # ── Title bar ──
        title_frame = tk.Frame(root, bg=self.BG)
        title_frame.pack(fill="x", padx=16, pady=(12, 0))
        tk.Label(title_frame, text="⚙  Settings", font=("Segoe UI Semibold", 13),
                 bg=self.BG, fg=self.FG).pack(side="left")
        tk.Label(title_frame, text=f"v{__version__}", font=("Segoe UI", 9),
                 bg=self.BG, fg=self.DIM).pack(side="right")

        # ── Two-column container ──
        columns = tk.Frame(root, bg=self.BG)
        columns.pack(fill="both", expand=True, padx=8, pady=(4, 0))
        left_col = tk.Frame(columns, bg=self.BG)
        left_col.pack(side="left", fill="both", expand=True, anchor="n")
        right_col = tk.Frame(columns, bg=self.BG)
        right_col.pack(side="left", fill="both", expand=True, anchor="n")

        # ══════════════════════════════════════════════════════════════════════
        # LEFT COLUMN
        # ══════════════════════════════════════════════════════════════════════

        # ── Voice & Playback ──
        sec = self._make_section(left_col, "Voice & Playback")

        all_voices = self._app.tts.voices
        self._en_codes = [v for v in all_voices if is_english_voice(v)]
        self._en_labels = [voice_display_name(v) for v in self._en_codes]
        self._es_codes = [v for v in all_voices if is_spanish_voice(v)]
        self._es_labels = [voice_display_name(v) for v in self._es_codes]

        r = 0
        self._add_label(sec, "English voice", r)
        self._voice_en_var = tk.StringVar(value=voice_display_name(self._app.tts.voice_en))
        self._add_combo(sec, self._voice_en_var, self._en_labels, r,
                        on_change=self._on_en_voice_changed)
        r += 1

        self._add_label(sec, "Spanish voice", r)
        self._voice_es_var = tk.StringVar(value=voice_display_name(self._app.tts.voice_es))
        self._add_combo(sec, self._voice_es_var, self._es_labels, r,
                        on_change=self._on_es_voice_changed)
        r += 1

        self._add_label(sec, "Speed", r)
        speed_labels = [format_speed(p) for p in SPEED_PRESETS]
        self._speed_var = tk.StringVar(value=format_speed(self._app.tts.current_speed))
        self._add_combo(sec, self._speed_var, speed_labels, r,
                        on_change=self._on_speed_changed)

        # ── Shortcuts ──
        sec = self._make_section(left_col, "Shortcuts")
        r = 0

        for label_text, attr_name, target_id in [
            ("Read aloud",  "_hotkey",           "read"),
            ("OCR region",  "_ocr_hotkey",       "ocr"),
            ("Dictation",   "_dictation_hotkey",  "dictation"),
            ("Grammar",     "_grammar_hotkey",    "grammar"),
        ]:
            self._add_label(sec, label_text, r)
            var = tk.StringVar(value=getattr(self._app, attr_name))
            setattr(self, f"{attr_name}_var", var)
            self._add_entry(sec, var, r)
            self._add_record_btn(sec, lambda t=target_id: self._start_hotkey_record(t), r)
            self._add_clear_btn(sec, var, r)
            r += 1

        # ── Grammar & Dictation ──
        sec = self._make_section(left_col, "Grammar & Dictation")
        r = 0

        self._add_label(sec, "Grammar mode", r)
        self._grammar_mode_var = tk.StringVar(value=self._app._grammar_mode)
        self._add_combo(sec, self._grammar_mode_var, GRAMMAR_MODES, r)
        r += 1

        self._add_label(sec, "Dictation provider", r)
        self._dictation_provider_var = tk.StringVar(value=self._app._dictation_provider)
        self._add_combo(sec, self._dictation_provider_var, DICTATION_PROVIDERS, r)
        r += 1

        self._add_label(sec, "Microphone", r)
        mic_names = self._get_mic_device_names()
        current_mic = self._app._mic_device or "System default"
        if current_mic not in mic_names:
            mic_names.insert(0, current_mic)
        self._mic_device_var = tk.StringVar(value=current_mic)
        self._add_combo(sec, self._mic_device_var, mic_names, r)
        r += 1

        self._add_label(sec, "Grammar provider", r)
        self._grammar_provider_var = tk.StringVar(value=self._app._grammar_provider)
        self._add_combo(sec, self._grammar_provider_var, GRAMMAR_PROVIDERS, r)

        # ── Anthropic API ──
        sec = self._make_section(left_col, "Anthropic API")
        r = 0

        self._add_label(sec, "API key", r)
        self._anthropic_api_key_var = tk.StringVar(value=self._app._anthropic_api_key)
        self._add_entry(sec, self._anthropic_api_key_var, r, show="●", state="normal")
        r += 1

        self._add_label(sec, "Model", r)
        self._anthropic_model_var = tk.StringVar(value=self._app._anthropic_model)
        self._add_entry(sec, self._anthropic_model_var, r, state="normal")

        # ══════════════════════════════════════════════════════════════════════
        # RIGHT COLUMN
        # ══════════════════════════════════════════════════════════════════════

        # ── Rephrase Styles ──
        sec = self._make_section(right_col, "Rephrase Styles")
        r = 0

        self._add_label(sec, "Active style", r)
        self._rephrase_style_var = tk.StringVar(value=self._app._rephrase_style)
        self._add_combo(sec, self._rephrase_style_var, REPHRASE_STYLES, r)
        r += 1

        # Per-style hotkeys
        style_hks = self._app._style_hotkeys
        self._style_hotkey_vars = {}
        for style_name in REPHRASE_STYLES:
            self._add_label(sec, f"  {style_name}", r)
            var = tk.StringVar(value=style_hks.get(style_name, ""))
            self._style_hotkey_vars[style_name] = var
            self._add_entry(sec, var, r)
            self._add_record_btn(
                sec, lambda s=style_name: self._start_hotkey_record(f"style_{s}"), r)
            self._add_clear_btn(sec, var, r)
            r += 1

        # Recall last style hotkey
        self._add_label(sec, "Recall last style", r)
        self._recall_style_hotkey_var = tk.StringVar(
            value=self._app._recall_style_hotkey)
        self._add_entry(sec, self._recall_style_hotkey_var, r)
        self._add_record_btn(sec, lambda: self._start_hotkey_record("recall_style"), r)
        self._add_clear_btn(sec, self._recall_style_hotkey_var, r)

        # ══════════════════════════════════════════════════════════════════════
        # Buttons
        # ══════════════════════════════════════════════════════════════════════
        btn_frame = tk.Frame(root, bg=self.BG)
        btn_frame.pack(pady=(10, 14))

        save_btn = tk.Button(btn_frame, text="  Save  ", font=("Segoe UI Semibold", 10),
                             bg=self.SAVE_BG, fg=self.SAVE_FG, relief="flat",
                             activebackground="#b8a0ff", cursor="hand2",
                             command=self._save)
        save_btn.pack(side="left", padx=8)

        cancel_btn = tk.Button(btn_frame, text="  Cancel  ", font=("Segoe UI", 10),
                               bg=self.BTN_BG, fg=self.BTN_FG, relief="flat",
                               activebackground="#6c7086", cursor="hand2",
                               command=self._on_close)
        cancel_btn.pack(side="left", padx=8)

        # Center on screen then show
        root.update_idletasks()
        w = max(root.winfo_reqwidth(), 700)
        h = root.winfo_reqheight()
        x = (root.winfo_screenwidth() - w) // 2
        y = (root.winfo_screenheight() - h) // 2
        root.geometry(f"{w}x{h}+{x}+{y}")
        root.deiconify()
        root.lift()
        root.focus_force()
        print(f"[Settings] Window opened {w}x{h} at +{x}+{y}", flush=True)

        if not parent:
            root.mainloop()

    # ── Hotkey Recording ─────────────────────────────────────────────────────

    def _start_hotkey_record(self, target):
        if self._recording:
            return
        self._recording = True
        self._record_target = target

        # Find the right var and set placeholder
        var = self._get_var_for_target(target)
        if var:
            self._prev_record_value = var.get()
            var.set("Press a key combo…")

        for b in self._record_buttons:
            b.config(state="disabled")

        def capture():
            try:
                combo = keyboard.read_hotkey(suppress=False)
                self._root.after(0, self._finish_record, combo)
            except Exception:
                self._root.after(0, self._finish_record, None)

        threading.Thread(target=capture, daemon=True).start()

    def _finish_record(self, combo):
        self._recording = False
        for b in self._record_buttons:
            b.config(state="normal")
        target = self._record_target
        if combo:
            var = self._get_var_for_target(target)
            if var:
                var.set(combo)
        else:
            var = self._get_var_for_target(target)
            if var and hasattr(self, "_prev_record_value"):
                var.set(self._prev_record_value)
        self._record_target = None

    def _get_var_for_target(self, target):
        """Return the StringVar for a given recording target."""
        mapping = {
            "read": self._hotkey_var,
            "ocr": self._ocr_hotkey_var,
            "dictation": self._dictation_hotkey_var,
            "grammar": self._grammar_hotkey_var,
            "recall_style": self._recall_style_hotkey_var,
        }
        if target in mapping:
            return mapping[target]
        if target and target.startswith("style_"):
            style_name = target[6:]
            return self._style_hotkey_vars.get(style_name)
        return None

    @staticmethod
    def _get_mic_device_names():
        """Return list of available input device names, with 'System default' first."""
        names = ["System default"]
        try:
            devices = sd.query_devices()
            for d in devices:
                if d["max_input_channels"] > 0:
                    names.append(d["name"])
        except Exception:
            pass
        return names

    # ── Voice / Speed Previews ───────────────────────────────────────────────

    def _on_en_voice_changed(self, event):
        label = self._voice_en_var.get()
        if label in self._en_labels:
            idx = self._en_labels.index(label)
            code = self._en_codes[idx]
            speed = parse_speed(self._speed_var.get())
            self._app.tts.stop()
            self._app.tts.set_speed(speed)
            self._app.tts.speak_preview("Hello! This is a preview.", code, "en-us")

    def _on_es_voice_changed(self, event):
        label = self._voice_es_var.get()
        if label in self._es_labels:
            idx = self._es_labels.index(label)
            code = self._es_codes[idx]
            speed = parse_speed(self._speed_var.get())
            self._app.tts.stop()
            self._app.tts.set_speed(speed)
            self._app.tts.speak_preview("Hola, esta es una vista previa.", code, "es")

    def _on_speed_changed(self, event):
        en_label = self._voice_en_var.get()
        if en_label in self._en_labels:
            idx = self._en_labels.index(en_label)
            code = self._en_codes[idx]
        else:
            code = self._app.tts.voice_en
        speed = parse_speed(self._speed_var.get())
        self._app.tts.stop()
        self._app.tts.set_speed(speed)
        self._app.tts.speak_preview("Hello! This is a preview.", code, "en-us")

    # ── Save ─────────────────────────────────────────────────────────────────

    def _save(self):
        # Resolve voices
        en_label = self._voice_en_var.get()
        idx_en = self._en_labels.index(en_label) if en_label in self._en_labels else 0
        voice_en = self._en_codes[idx_en]

        es_label = self._voice_es_var.get()
        idx_es = self._es_labels.index(es_label) if es_label in self._es_labels else 0
        voice_es = self._es_codes[idx_es]

        speed = parse_speed(self._speed_var.get())
        hotkey = self._hotkey_var.get()
        ocr_hotkey = self._ocr_hotkey_var.get().strip()
        dictation_hotkey = self._dictation_hotkey_var.get()
        grammar_hotkey = self._grammar_hotkey_var.get()
        rephrase_hotkey = ""
        rephrase_style = self._rephrase_style_var.get() or DEFAULT_REPHRASE_STYLE
        recall_style_hotkey = self._recall_style_hotkey_var.get().strip()
        grammar_mode = self._grammar_mode_var.get()
        dictation_provider = self._dictation_provider_var.get()
        grammar_provider = self._grammar_provider_var.get()
        mic_choice = self._mic_device_var.get()
        mic_device = "" if mic_choice == "System default" else mic_choice
        anthropic_api_key = self._anthropic_api_key_var.get().strip()
        anthropic_model = self._anthropic_model_var.get().strip() or ANTHROPIC_MODEL_DEFAULT

        # Collect per-style hotkeys
        style_hotkeys = {}
        for style_name in REPHRASE_STYLES:
            val = self._style_hotkey_vars[style_name].get().strip()
            if val and val != "Press a key combo…":
                style_hotkeys[style_name] = val
            else:
                style_hotkeys[style_name] = ""

        # Conflict check — collect all non-empty hotkeys
        all_shortcuts = {
            "Read-aloud": hotkey,
            "Dictation": dictation_hotkey,
            "Grammar": grammar_hotkey,
        }
        if recall_style_hotkey:
            all_shortcuts["Recall last style"] = recall_style_hotkey
        for sn, sh in style_hotkeys.items():
            if sh:
                all_shortcuts[f"Rephrase ({sn})"] = sh

        seen = {}
        for name, key in all_shortcuts.items():
            if not key:
                continue
            if key in seen:
                messagebox.showerror("Shortcut conflict",
                    f"{name} and {seen[key]} shortcuts must be different.",
                    parent=self._root)
                return
            seen[key] = name

        if grammar_mode not in GRAMMAR_MODES:
            messagebox.showerror("Invalid setting", "Please select a valid grammar mode.", parent=self._root)
            return
        if dictation_provider not in DICTATION_PROVIDERS:
            messagebox.showerror("Invalid setting", "Please select a valid dictation provider.", parent=self._root)
            return
        if grammar_provider not in GRAMMAR_PROVIDERS:
            messagebox.showerror("Invalid setting", "Please select a valid grammar provider.", parent=self._root)
            return

        # Apply
        self._app.tts.set_voice_en(voice_en)
        self._app.tts.set_voice_es(voice_es)
        self._app.set_reading_speed(speed, persist=False)

        if hotkey != self._app._hotkey:
            self._app._update_hotkey(hotkey)
        if ocr_hotkey != self._app._ocr_hotkey:
            self._app._update_ocr_hotkey(ocr_hotkey)
        if dictation_hotkey != self._app._dictation_hotkey:
            self._app._update_dictation_hotkey(dictation_hotkey)
        if grammar_hotkey != self._app._grammar_hotkey:
            self._app._update_grammar_hotkey(grammar_hotkey)
        if rephrase_hotkey != self._app._rephrase_hotkey:
            if self._app._rephrase_hotkey_handle is not None:
                keyboard.remove_hotkey(self._app._rephrase_hotkey_handle)
                self._app._rephrase_hotkey_handle = None
            self._app._rephrase_hotkey = rephrase_hotkey
        self._app._grammar_mode = grammar_mode
        self._app._dictation_provider = dictation_provider
        self._app._grammar_provider = grammar_provider
        self._app._mic_device = mic_device
        self._app._apply_mic_device()
        self._app._rephrase_style = rephrase_style
        self._app._style_hotkeys = style_hotkeys
        self._app._recall_style_hotkey = recall_style_hotkey
        self._app._register_style_hotkeys()
        _sb = FloatingStatusBar.get()
        if _sb:
            _sb.sync_style()
        self._app._anthropic_api_key = anthropic_api_key
        self._app._anthropic_model = anthropic_model
        self._app._refresh_menu()

        save_config({
            "hotkey": hotkey,
            "ocr_hotkey": ocr_hotkey,
            "dictation_hotkey": dictation_hotkey,
            "grammar_hotkey": grammar_hotkey,
            "rephrase_hotkey": rephrase_hotkey,
            "rephrase_style": rephrase_style,
            "style_hotkeys": style_hotkeys,
            "recall_style_hotkey": recall_style_hotkey,
            "voice_en": voice_en,
            "voice_es": voice_es,
            "speed": speed,
            "grammar_mode": grammar_mode,
            "dictation_provider": dictation_provider,
            "grammar_provider": grammar_provider,
            "mic_device": mic_device,
            "anthropic_api_key": anthropic_api_key,
            "anthropic_model": anthropic_model,
        })

        self._on_close()

    def _on_close(self):
        with SettingsWindow._instance_lock:
            SettingsWindow._instance = None
        try:
            self._root.destroy()
        except Exception:
            pass


# ── TTS Worker ───────────────────────────────────────────────────────────────

class TTSWorker:
    def __init__(self):
        self._queue = queue.Queue()
        self._stop_event = threading.Event()
        self._pause_wait = threading.Event()
        self._pause_wait.set()
        self._speaking = False
        self._paused = False
        self._session_active = False
        self._voices = []
        self._voice_en = DEFAULT_VOICE_EN
        self._voice_es = DEFAULT_VOICE_ES
        self._current_speed = DEFAULT_SPEED
        self._sentences = []
        self._sentence_idx = 0
        self._playhead = 0
        self._play_gen = 0
        self._current_sentence_audio = None
        self._cached_sentence_idx = -1
        self._current_sample_rate = 24000
        self.on_state_change = None
        self.on_playback_change = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    @property
    def is_speaking(self):
        return self._speaking

    @property
    def is_active(self):
        return self._session_active

    @property
    def is_paused(self):
        return self._paused and self._session_active

    @property
    def voices(self):
        return list(self._voices)

    @property
    def voice_en(self):
        return self._voice_en

    @property
    def voice_es(self):
        return self._voice_es

    @property
    def current_speed(self):
        return self._current_speed

    def _notify_playback(self):
        if self.on_playback_change:
            try:
                self.on_playback_change()
            except Exception:
                pass

    def speak(self, text):
        lang = detect_language(text)
        voice = self._voice_es if lang == "es" else self._voice_en
        kokoro_lang = _KOKORO_LANG.get(lang, "en-us")
        self._queue.put(("speak", (text, voice, kokoro_lang)))

    def speak_preview(self, text, voice, kokoro_lang):
        """Speak a preview with an explicit voice and lang (skips detection)."""
        self._queue.put(("speak", (text, voice, kokoro_lang)))

    def stop(self):
        self._stop_event.set()
        self._session_active = False
        self._paused = False
        self._pause_wait.set()
        self._play_gen += 1
        self._current_sentence_audio = None
        self._cached_sentence_idx = -1
        if self._speaking:
            self._set_speaking(False)
        self._notify_playback()

    def pause(self):
        if not self._session_active or self._paused:
            return
        self._paused = True
        self._pause_wait.clear()
        self._notify_playback()

    def resume(self):
        if not self._session_active or not self._paused:
            return
        self._paused = False
        self._pause_wait.set()
        self._notify_playback()

    def next_sentence(self):
        if not self._session_active:
            return
        if self._sentence_idx < len(self._sentences) - 1:
            self._sentence_idx += 1
            self._playhead = 0
            self._current_sentence_audio = None
            self._cached_sentence_idx = -1
            self._play_gen += 1
            self._paused = False
            self._pause_wait.set()
            self._notify_playback()
        else:
            self.stop()

    def prev_sentence(self):
        if not self._session_active:
            return
        if self._sentence_idx > 0:
            self._sentence_idx -= 1
            self._playhead = 0
            self._current_sentence_audio = None
            self._cached_sentence_idx = -1
            self._play_gen += 1
            self._paused = False
            self._pause_wait.set()
            self._notify_playback()
        else:
            self._playhead = 0
            self._play_gen += 1
            self._paused = False
            self._pause_wait.set()
            self._notify_playback()

    def skip_seconds(self, delta):
        if not self._session_active:
            return
        audio = self._current_sentence_audio
        if audio is None or len(audio) == 0:
            return
        sr = self._current_sample_rate or 24000
        new_head = self._playhead + int(delta * sr)
        if new_head < 0:
            new_head = 0
        if new_head >= len(audio):
            self.next_sentence()
            return
        self._playhead = new_head
        self._play_gen += 1
        self._paused = False
        self._pause_wait.set()
        self._notify_playback()

    def set_voice_en(self, voice):
        self._queue.put(("set_voice_en", voice))

    def set_voice_es(self, voice):
        self._queue.put(("set_voice_es", voice))

    def set_speed(self, speed):
        self._queue.put(("set_speed", speed))

    def quit(self):
        self._queue.put(("quit", None))

    def _set_speaking(self, value):
        self._speaking = value
        if self.on_state_change:
            try:
                self.on_state_change(value)
            except Exception:
                pass

    def _run(self):
        if USE_GPU:
            use_providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
            model_path = MODEL_PATH_FP16
            print("Using GPU (CUDA) with fp16 model for TTS inference.")
        else:
            use_providers = ["CPUExecutionProvider"]
            model_path = MODEL_PATH_INT8
            print("Using CPU with int8 model for TTS inference.")
        session = ort.InferenceSession(model_path, providers=use_providers)
        kokoro = Kokoro.from_session(session, VOICES_PATH)

        data = np.load(VOICES_PATH)
        self._voices = sorted(data.files)
        data.close()

        asyncio.run(self._warmup(kokoro))

        while True:
            cmd, arg = self._queue.get()

            try:
                if cmd == "speak":
                    text, voice, kokoro_lang = arg
                    self._speak_session(kokoro, text, voice, kokoro_lang)

                elif cmd == "set_voice_en":
                    self._voice_en = arg

                elif cmd == "set_voice_es":
                    self._voice_es = arg

                elif cmd == "set_speed":
                    self._current_speed = arg

                elif cmd == "quit":
                    break

            except Exception as e:
                print(f"TTS error: {e}", file=sys.stderr)
                self._session_active = False
                self._set_speaking(False)
                self._notify_playback()

    def _speak_session(self, kokoro, text, voice, kokoro_lang):
        self._sentences = split_sentences(text)
        if not self._sentences:
            return

        self._sentence_idx = 0
        self._playhead = 0
        self._session_active = True
        self._stop_event.clear()
        self._paused = False
        self._pause_wait.set()
        self._current_sentence_audio = None
        self._cached_sentence_idx = -1
        self._set_speaking(True)
        self._notify_playback()

        while self._session_active and self._sentence_idx < len(self._sentences):
            if self._stop_event.is_set():
                break

            if self._cached_sentence_idx != self._sentence_idx:
                sent = self._sentences[self._sentence_idx]
                audio, sr = asyncio.run(
                    self._synthesize_sentence(kokoro, sent, voice, kokoro_lang)
                )
                self._current_sentence_audio = audio
                self._cached_sentence_idx = self._sentence_idx
                self._current_sample_rate = sr

            audio = self._current_sentence_audio
            if audio is None or len(audio) == 0:
                self._sentence_idx += 1
                self._playhead = 0
                self._cached_sentence_idx = -1
                continue

            gen = self._play_gen
            completed = self._play_audio_buffer(
                audio, self._current_sample_rate, self._playhead, gen
            )

            if self._stop_event.is_set():
                break
            if self._play_gen != gen:
                continue
            if not completed:
                break

            self._sentence_idx += 1
            self._playhead = 0
            self._cached_sentence_idx = -1

        self._session_active = False
        self._paused = False
        self._pause_wait.set()
        self._current_sentence_audio = None
        self._set_speaking(False)
        self._notify_playback()

    async def _warmup(self, kokoro):
        async for _, _ in kokoro.create_stream(".", voice=self._voice_en, speed=1.0, lang="en-us"):
            break

    async def _synthesize_sentence(self, kokoro, text, voice, kokoro_lang):
        chunks = []
        sample_rate = 24000
        stream = kokoro.create_stream(
            text, voice=voice, speed=self._current_speed, lang=kokoro_lang
        )
        async for samples, sample_rate in stream:
            if self._stop_event.is_set():
                break
            chunks.append(np.asarray(samples, dtype=np.float32))
        if not chunks:
            return np.array([], dtype=np.float32), sample_rate
        return np.concatenate(chunks), sample_rate

    def _play_audio_buffer(self, samples, sample_rate, start_sample, generation):
        """Play audio with pause, stop, and seek support."""
        samples = np.asarray(samples, dtype=np.float32)
        chunk_size = max(1, int(sample_rate * AUDIO_CHUNK_SECS))
        try:
            with sd.OutputStream(samplerate=sample_rate, channels=1, dtype="float32") as out:
                i = start_sample
                while i < len(samples):
                    if self._stop_event.is_set() or self._play_gen != generation:
                        return False
                    while self._paused:
                        self._pause_wait.wait(timeout=0.05)
                        if self._stop_event.is_set() or self._play_gen != generation:
                            return False
                    if self._playhead != i:
                        i = min(self._playhead, len(samples))
                    end = min(i + chunk_size, len(samples))
                    out.write(samples[i:end].reshape(-1, 1))
                    i = end
                    self._playhead = i
        except Exception as e:
            print(f"Audio playback error: {e}", file=sys.stderr)
            return False
        return True


# ── Main App ─────────────────────────────────────────────────────────────────

class TinyReadAloud:
    def __init__(self):
        cfg = load_config()
        self._hotkey = cfg["hotkey"]
        self._ocr_hotkey = cfg.get("ocr_hotkey", OCR_HOTKEY)
        self._dictation_hotkey = cfg["dictation_hotkey"]
        self._grammar_hotkey = cfg["grammar_hotkey"]
        self._rephrase_hotkey = cfg["rephrase_hotkey"]
        self._grammar_mode = cfg["grammar_mode"]
        self._dictation_provider = cfg["dictation_provider"]
        self._grammar_provider = cfg["grammar_provider"]
        self._mic_device = cfg.get("mic_device", DEFAULT_MIC_DEVICE)
        self._rephrase_style = cfg["rephrase_style"]
        self._style_hotkeys = cfg.get("style_hotkeys", dict(DEFAULT_STYLE_HOTKEYS))
        self._recall_style_hotkey = cfg.get("recall_style_hotkey", RECALL_STYLE_HOTKEY)
        self._last_rephrase_style = self._rephrase_style
        self._anthropic_api_key = cfg["anthropic_api_key"]
        self._anthropic_model = cfg["anthropic_model"]
        self.tts = TTSWorker()
        self.tts._voice_en = cfg["voice_en"]
        self.tts._voice_es = cfg["voice_es"]
        self.tts._current_speed = clamp_speed(cfg["speed"])
        self.tts.on_state_change = self._on_speaking_changed
        self.tts.on_playback_change = self._on_playback_changed
        self.icon = None
        self._hotkey_handle = None
        self._ocr_hotkey_handle = None
        self._dictation_hotkey_handle = None
        self._grammar_hotkey_handle = None
        self._rephrase_hotkey_handle = None
        self._style_hotkey_handles = {}
        self._recall_style_hotkey_handle = None
        self._status_bar = None
        self._dictation_listening = False
        self._update_info = None
        self._grammar_cancel = threading.Event()
        self._rephrase_cancel = threading.Event()
        self._grammar_thread = None
        self._rephrase_thread = None
        self._anykey_hook = None
        self._stopped_by_key = False
        self._speaking_since = 0
        self._dictation_anykey_hook = None
        self._dictation_started_at = 0
        self._icon_idle = create_tray_icon(speaking=False)
        self._icon_speaking = create_tray_icon(speaking=True)
        self._health_report = None
        self._ocr_in_progress = False

    def run(self):
        self.icon = pystray.Icon(
            name="TinyReadAloud",
            icon=self._icon_idle,
            title=f"TinyReadAloud v{__version__}  [Read: {self._hotkey} | OCR: {self._ocr_hotkey} | Dictation: {self._dictation_hotkey} | Grammar: {self._grammar_hotkey} | Rephrase: {self._rephrase_hotkey}]",
            menu=self._build_menu(),
        )
        self.icon.run(setup=self._on_ready)

    def _safe_add_hotkey(self, hotkey, callback, label):
        if not hotkey:
            return None
        try:
            return keyboard.add_hotkey(hotkey, callback, suppress=False)
        except Exception as e:
            print(f"[Hotkey] Failed to register {label} ({hotkey}): {e}", flush=True)
            if self.icon:
                self.icon.notify(
                    f"Could not register {label} hotkey ({hotkey}). "
                    "Try running as administrator or change the shortcut in Settings.",
                    "TinyReadAloud",
                )
            return None

    def _on_ready(self, icon):
        icon.visible = True
        self.tts.start()
        self._apply_mic_device()
        time.sleep(1.0)  # give Kokoro time to load
        icon.menu = self._build_menu()
        icon.update_menu()
        self._hotkey_handle = self._safe_add_hotkey(
            self._hotkey, self._on_hotkey, "read")
        self._ocr_hotkey_handle = self._safe_add_hotkey(
            self._ocr_hotkey, self._on_ocr_hotkey, "OCR")
        self._dictation_hotkey_handle = self._safe_add_hotkey(
            self._dictation_hotkey, self._on_dictation_hotkey, "dictation")
        self._grammar_hotkey_handle = self._safe_add_hotkey(
            self._grammar_hotkey, self._on_grammar_hotkey, "grammar")
        self._rephrase_hotkey_handle = self._safe_add_hotkey(
            self._rephrase_hotkey, self._on_rephrase_hotkey, "rephrase")
        self._register_style_hotkeys()
        FloatingStatusBar.open(self)
        self._status_bar = FloatingStatusBar.get()
        threading.Thread(target=self._run_startup_health_check, daemon=True).start()
        print(
            f"TinyReadAloud v{__version__} ready. "
            f"Read: {self._hotkey} | OCR: {self._ocr_hotkey} | Dictation: {self._dictation_hotkey} | Grammar: {self._grammar_hotkey} | Rephrase: {self._rephrase_hotkey}"
        )
        # Check for updates in background after 5 seconds
        threading.Timer(5.0, self._check_for_updates_background).start()

    def _run_startup_health_check(self):
        try:
            self._health_report = run_health_check(include_clipboard=False)
            if not self._health_report.ok and self.icon:
                self.icon.notify(
                    f"OCR may not work: {self._health_report.summary()}",
                    "TinyReadAloud",
                )
        except Exception as exc:
            log_exception("Health", exc)

    def _ocr_user_message(self, result) -> str:
        err = (result.error or "").lower()
        if "language" in err or "engine" in err:
            return (
                "Windows OCR is not available. Add an English language pack "
                "in Settings → Time & language → Language."
            )
        if err == "screenshot_failed":
            return "Could not capture that screen region. Try again on a visible area."
        if err == "winrt_not_installed":
            return "OCR packages missing. Run: pip install -r requirements.txt"
        return (
            "OCR found no text. Select a larger area with clear text. "
            f"Debug crop saved under %LOCALAPPDATA%\\TinyReadAloud\\ocr_debug"
        )

    def _build_menu(self):
        playback_active = lambda item: self.tts.is_active
        items = [
            pystray.MenuItem(
                "Pause",
                self._cmd_pause,
                enabled=lambda item: self.tts.is_active and not self.tts.is_paused,
            ),
            pystray.MenuItem(
                "Resume",
                self._cmd_resume,
                enabled=lambda item: self.tts.is_paused,
            ),
            pystray.MenuItem(
                "Stop",
                self._cmd_stop,
                enabled=playback_active,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Read Region (OCR)…", self._cmd_ocr_region),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Next Sentence",
                self._cmd_next_sentence,
                enabled=playback_active,
            ),
            pystray.MenuItem(
                "Previous Sentence",
                self._cmd_prev_sentence,
                enabled=playback_active,
            ),
            pystray.MenuItem(
                f"Skip +{SKIP_SECONDS} sec",
                self._cmd_skip_forward,
                enabled=playback_active,
            ),
            pystray.MenuItem(
                f"Skip -{SKIP_SECONDS} sec",
                self._cmd_skip_back,
                enabled=playback_active,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Stop Dictation" if self._dictation_listening else "Start Dictation",
                self._cmd_toggle_dictation,
            ),
            pystray.MenuItem(
                "Check Grammar",
                self._cmd_check_grammar,
                enabled=lambda item: self._grammar_mode != "off",
            ),
            pystray.MenuItem(
                "Rephrase",
                self._cmd_rephrase,
            ),
        ]

        # English voice submenu
        if self.tts.voices:
            en_items = []
            for v in self.tts.voices:
                if is_english_voice(v):
                    en_items.append(
                        pystray.MenuItem(
                            voice_display_name(v),
                            self._make_voice_en_setter(v),
                            checked=lambda item, vc=v: self.tts.voice_en == vc,
                            radio=True,
                        )
                    )
            if en_items:
                items.append(pystray.MenuItem("English Voice", pystray.Menu(*en_items)))

            # Spanish voice submenu
            es_items = []
            for v in self.tts.voices:
                if is_spanish_voice(v):
                    es_items.append(
                        pystray.MenuItem(
                            voice_display_name(v),
                            self._make_voice_es_setter(v),
                            checked=lambda item, vc=v: self.tts.voice_es == vc,
                            radio=True,
                        )
                    )
            if es_items:
                items.append(pystray.MenuItem("Spanish Voice", pystray.Menu(*es_items)))

        # Speed submenu
        speed_items = [
            pystray.MenuItem(
                format_speed(spd),
                self._make_speed_setter(spd),
                checked=lambda item, s=spd: abs(self.tts.current_speed - s) < 0.05,
                radio=True,
            )
            for spd in SPEED_PRESETS
        ]
        items.append(pystray.MenuItem("Speed", pystray.Menu(*speed_items)))

        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("Settings", self._cmd_settings))
        items.append(pystray.MenuItem("Check for Updates", self._cmd_check_updates))
        if self._update_info and self._update_info.available:
            items.append(pystray.MenuItem(
                f"Download v{self._update_info.tag}",
                self._cmd_download_update))
        items.append(pystray.MenuItem("Exit", self._cmd_exit))

        return pystray.Menu(*items)

    def _make_voice_en_setter(self, voice):
        def setter(icon, item):
            self.tts.set_voice_en(voice)
        return setter

    def _make_voice_es_setter(self, voice):
        def setter(icon, item):
            self.tts.set_voice_es(voice)
        return setter

    def _make_speed_setter(self, speed):
        def setter(icon, item):
            self.set_reading_speed(speed)
        return setter

    def set_reading_speed(self, speed, persist=True):
        """Apply TTS speed and sync toolbar, tray menu, and config."""
        speed = clamp_speed(speed)
        self.tts.set_speed(speed)
        sb = FloatingStatusBar.get()
        if sb:
            sb.sync_speed(speed)
        if persist:
            try:
                cfg = load_config()
                cfg["speed"] = speed
                save_config(cfg)
            except Exception:
                pass
        self._refresh_menu()

    def _on_hotkey(self):
        target_hwnd = user32.GetForegroundWindow()
        if self._status_bar is not None:
            if target_hwnd == self._status_bar._own_hwnd:
                target_hwnd = self._status_bar._last_target_hwnd or target_hwnd
            elif self._status_bar._last_target_hwnd:
                target_hwnd = self._status_bar._last_target_hwnd
        self.start_read(target_hwnd)

    def _on_ocr_hotkey(self):
        self.start_ocr_read()

    def start_ocr_read(self):
        """Dim screen and let the user drag a region to OCR and read."""
        if self._ocr_in_progress:
            self._set_status("OCR already in progress…")
            return
        if self._health_report is not None and not self._health_report.ok:
            msg = self._health_report.summary()
            self._set_status("OCR unavailable.")
            if self.icon:
                self.icon.notify(msg, "TinyReadAloud — OCR")
            return
        if self.tts.is_active:
            self.tts.stop()
        self._ocr_in_progress = True
        parent = None
        if self._status_bar and self._status_bar._root:
            self._status_bar.prepare_for_ocr_overlay()
            parent = self._status_bar._root
        RegionSelectOverlay.pick(self._on_ocr_region_picked, parent_root=parent)

    def _on_ocr_region_picked(self, bbox):
        try:
            if not bbox:
                self._set_status("OCR cancelled.")
                return
            threading.Thread(
                target=self._ocr_region_and_speak,
                args=(bbox,),
                daemon=True,
            ).start()
        finally:
            self._ocr_in_progress = False
            if self._status_bar:
                self._status_bar.restore_after_ocr_overlay()

    def _ocr_region_and_speak(self, bbox):
        trace_id = new_trace_id()
        log_event("OCR", "region start", trace_id=trace_id, bbox=bbox)
        try:
            time.sleep(0.25)
            self._set_status("OCR scanning…")
            result = capture_ocr_region(bbox, trace_id=trace_id)
            log_event(
                "OCR", "region done",
                trace_id=trace_id, ok=result.ok, attempts=result.attempts,
            )
            if not result.ok:
                self._set_status("No text in region.")
                if self.icon:
                    self.icon.notify(self._ocr_user_message(result), "TinyReadAloud")
                return
            self._set_status("Read (OCR)…")
            self._reset_hotkeys()
            self.tts.speak(result.text)
        except Exception as exc:
            log_exception("OCR", exc, trace_id)
            self._set_status("OCR error.")
            if self.icon:
                self.icon.notify(f"OCR error: {exc}", "TinyReadAloud")

    def start_read(self, target_hwnd=None):
        """Start reading selection, or stop if already playing."""
        if self.tts.is_active:
            self.tts.stop()
            return
        if not target_hwnd:
            target_hwnd = (
                self._status_bar._last_target_hwnd
                if self._status_bar is not None
                else user32.GetForegroundWindow()
            ) or user32.GetForegroundWindow()
        threading.Thread(
            target=self._capture_and_speak,
            args=(target_hwnd,),
            daemon=True,
        ).start()

    def _on_dictation_hotkey(self):
        threading.Thread(target=self._toggle_dictation, daemon=True).start()

    def _on_grammar_hotkey(self):
        if self._grammar_mode == "off":
            return
        # Cancel any in-progress grammar or rephrase operation
        self._grammar_cancel.set()
        self._rephrase_cancel.set()
        self.tts.stop()
        target_hwnd = (
            self._status_bar._last_target_hwnd
            if self._status_bar is not None
            else user32.GetForegroundWindow()
        ) or user32.GetForegroundWindow()
        print(f"[Grammar] Hotkey fired. target_hwnd={target_hwnd:#010x}", flush=True)
        t = threading.Thread(target=self._run_grammar_select_all, args=(target_hwnd,), daemon=True)
        self._grammar_thread = t
        t.start()

    def _on_rephrase_hotkey(self):
        # NOTE: this runs in the keyboard hook thread — must return in <300ms or
        # Windows kills the hook. No blocking calls (icon.notify, sleeps, etc.) here.
        self._rephrase_cancel.set()
        self._grammar_cancel.set()
        self.tts.stop()
        target_hwnd = (
            self._status_bar._last_target_hwnd
            if self._status_bar is not None
            else user32.GetForegroundWindow()
        ) or user32.GetForegroundWindow()
        style = self._rephrase_style
        self._last_rephrase_style = style
        print(f"[Rephrase] Hotkey fired. target_hwnd={target_hwnd:#010x}  style={style!r}", flush=True)
        t = threading.Thread(target=self._run_rephrase_select_all, args=(target_hwnd,), daemon=True)
        self._rephrase_thread = t
        t.start()

    def _register_style_hotkeys(self):
        """Register per-style hotkeys and recall-last-style hotkey."""
        # Remove old handles
        for h in self._style_hotkey_handles.values():
            try:
                keyboard.remove_hotkey(h)
            except Exception:
                pass
        self._style_hotkey_handles.clear()
        if self._recall_style_hotkey_handle is not None:
            try:
                keyboard.remove_hotkey(self._recall_style_hotkey_handle)
            except Exception:
                pass
            self._recall_style_hotkey_handle = None

        # Register per-style hotkeys
        for style, hk in self._style_hotkeys.items():
            if hk and hk.strip():
                try:
                    self._style_hotkey_handles[style] = keyboard.add_hotkey(
                        hk, self._on_style_hotkey, args=(style,), suppress=False
                    )
                except Exception as e:
                    print(f"[StyleHotkey] Failed to register {style}={hk}: {e}", flush=True)

        # Register recall-last-style hotkey
        if self._recall_style_hotkey and self._recall_style_hotkey.strip():
            try:
                self._recall_style_hotkey_handle = keyboard.add_hotkey(
                    self._recall_style_hotkey, self._on_recall_style_hotkey, suppress=False
                )
            except Exception as e:
                print(f"[StyleHotkey] Failed to register recall={self._recall_style_hotkey}: {e}", flush=True)

    def _on_style_hotkey(self, style):
        """Rephrase using a specific style via its dedicated hotkey."""
        self._last_rephrase_style = self._rephrase_style
        self._rephrase_style = style
        sb = FloatingStatusBar.get()
        if sb:
            sb.sync_style()
        self._rephrase_cancel.set()
        self._grammar_cancel.set()
        self.tts.stop()
        target_hwnd = (
            self._status_bar._last_target_hwnd
            if self._status_bar is not None
            else user32.GetForegroundWindow()
        ) or user32.GetForegroundWindow()
        print(f"[Rephrase] Style hotkey fired. style={style!r} target={target_hwnd:#010x}", flush=True)
        t = threading.Thread(target=self._run_rephrase_select_all, args=(target_hwnd,), daemon=True)
        self._rephrase_thread = t
        t.start()

    def _on_recall_style_hotkey(self):
        """Switch back to the last used rephrase style and trigger rephrase."""
        prev = self._last_rephrase_style
        self._last_rephrase_style = self._rephrase_style
        self._rephrase_style = prev
        sb = FloatingStatusBar.get()
        if sb:
            sb.sync_style()
        self._rephrase_cancel.set()
        self._grammar_cancel.set()
        self.tts.stop()
        target_hwnd = (
            self._status_bar._last_target_hwnd
            if self._status_bar is not None
            else user32.GetForegroundWindow()
        ) or user32.GetForegroundWindow()
        print(f"[Rephrase] Recall style hotkey fired. style={prev!r} target={target_hwnd:#010x}", flush=True)
        t = threading.Thread(target=self._run_rephrase_select_all, args=(target_hwnd,), daemon=True)
        self._rephrase_thread = t
        t.start()

    def _capture_and_speak(self, target_hwnd=None):
        trace_id = new_trace_id()
        try:
            if not target_hwnd:
                target_hwnd = (
                    self._status_bar._last_target_hwnd
                    if self._status_bar is not None
                    else user32.GetForegroundWindow()
                ) or user32.GetForegroundWindow()
            log_event("Read", "capture start", trace_id=trace_id, hwnd=f"{target_hwnd:#010x}")

            self._set_status("Capturing text…")
            text = capture_selected_text(target_hwnd)
            if not text:
                self._set_status("Trying accessibility…")
                text = capture_via_accessibility(target_hwnd)
            log_event("Read", "capture done", trace_id=trace_id, chars=len(text))

            if not text:
                self._set_status("Nothing selected.")
                if self.icon:
                    self.icon.notify(
                        "No text captured. Highlight text first, or use OCR "
                        f"({self._ocr_hotkey} or toolbar OCR button).",
                        "TinyReadAloud",
                    )
                return

            self._reset_hotkeys()
            self.tts.speak(text)
        except Exception as exc:
            log_exception("Read", exc, trace_id)
            self._set_status("Read error.")
            if self.icon:
                self.icon.notify(f"Read failed: {exc}", "TinyReadAloud")

    def _on_speaking_changed(self, is_speaking):
        if self.icon:
            self.icon.icon = (
                self._icon_speaking if is_speaking else self._icon_idle
            )
        self._update_playback_status()
        # Any-key-stops-playback (not while paused)
        if is_speaking and not self.tts.is_paused:
            self._speaking_since = time.monotonic()
            try:
                self._anykey_hook = keyboard.on_press(self._on_anykey_stop)
            except Exception:
                pass
        else:
            hook = self._anykey_hook
            if hook is not None:
                try:
                    keyboard.unhook(hook)
                except (KeyError, ValueError):
                    pass
                self._anykey_hook = None
            # After stopping via any-key press, run grammar check
            if self._stopped_by_key:
                self._stopped_by_key = False
                if self._grammar_mode != "off":
                    target_hwnd = (
                        self._status_bar._last_target_hwnd
                        if self._status_bar is not None
                        else user32.GetForegroundWindow()
                    ) or user32.GetForegroundWindow()
                    self._grammar_cancel.set()
                    t = threading.Thread(
                        target=self._run_grammar_select_all,
                        args=(target_hwnd,), daemon=True,
                    )
                    self._grammar_thread = t
                    t.start()

    def _on_playback_changed(self):
        self._update_playback_status()
        if self._status_bar is not None:
            self._status_bar.update_playback_controls()
        if self.icon:
            try:
                self.icon.update_menu()
            except Exception:
                pass

    def _update_playback_status(self):
        if self.tts.is_paused:
            self._set_status("Paused")
        elif self.tts.is_active:
            self._set_status("Speaking…")
        else:
            self._set_status("Ready")

    def _on_anykey_stop(self, event):
        """Stop TTS playback when any key is pressed."""
        if self.tts.is_paused:
            return
        if time.monotonic() - self._speaking_since < 1.0:
            return  # ignore keys from the hotkey that triggered playback
        name = (getattr(event, "name", "") or "").lower()
        if name in {
            "ctrl", "alt", "shift",
            "left ctrl", "right ctrl", "left alt", "right alt",
            "left shift", "right shift",
        }:
            return
        self._stopped_by_key = True
        self.tts.stop()

    def _on_anykey_stop_dictation(self, event):
        """Stop dictation when any key is pressed during listening."""
        if time.monotonic() - self._dictation_started_at < 0.8:
            return  # ignore keys from the hotkey that started dictation
        # Run toggle in a separate thread (sends Win+H and triggers grammar)
        threading.Thread(target=self._toggle_dictation, daemon=True).start()

    def _unhook_dictation_anykey(self):
        hook = self._dictation_anykey_hook
        if hook is not None:
            try:
                keyboard.unhook(hook)
            except (KeyError, ValueError):
                pass
            self._dictation_anykey_hook = None

    def _set_status(self, text):
        """Update the floating status bar (no-op if not open)."""
        if self._status_bar is not None:
            self._status_bar.set_status(text)

    def _cmd_stop(self, icon, item):
        self.tts.stop()
        self._grammar_cancel.set()
        self._rephrase_cancel.set()

    def _cmd_ocr_region(self, icon, item):
        self.start_ocr_read()

    def _cmd_pause(self, icon, item):
        self.tts.pause()

    def _cmd_resume(self, icon, item):
        self.tts.resume()

    def _cmd_next_sentence(self, icon, item):
        self.tts.next_sentence()

    def _cmd_prev_sentence(self, icon, item):
        self.tts.prev_sentence()

    def _cmd_skip_forward(self, icon, item):
        self.tts.skip_seconds(SKIP_SECONDS)

    def _cmd_skip_back(self, icon, item):
        self.tts.skip_seconds(-SKIP_SECONDS)

    def _cmd_toggle_dictation(self, icon, item):
        self._toggle_dictation()

    def _apply_mic_device(self):
        """Set the sounddevice default input device from config."""
        if self._mic_device:
            try:
                devices = sd.query_devices()
                for i, d in enumerate(devices):
                    if d["name"] == self._mic_device and d["max_input_channels"] > 0:
                        # Only set input device; preserve output device
                        cur_out = sd.default.device
                        # Extract raw output index from _InputOutputPair or plain value
                        if hasattr(cur_out, 'output'):
                            out_idx = cur_out.output
                        elif hasattr(cur_out, '__getitem__') and not isinstance(cur_out, (int, str)):
                            out_idx = cur_out[1]
                        else:
                            out_idx = None
                        sd.default.device = [i, out_idx]
                        print(f"[Mic] Set input device to: {self._mic_device} (index {i})", flush=True)
                        return
                print(f"[Mic] Device not found: {self._mic_device!r}, using system default.", flush=True)
            except Exception as e:
                print(f"[Mic] Error setting device: {e}", flush=True)

    def _toggle_dictation(self):
        try:
            # When starting dictation, verify a text field is focused
            if not self._dictation_listening and not _is_textfield_focused():
                self._set_status("No text field focused — click a text field first")
                if self.icon:
                    self.icon.notify(
                        "Focus a text field before starting dictation.",
                        "TinyReadAloud",
                    )
                return
            if self._dictation_provider == "windows":
                _wait_for_modifiers_released()
                keyboard.send("windows+h")
            else:
                raise RuntimeError(f"Unsupported dictation provider: {self._dictation_provider}")
            self._dictation_listening = not self._dictation_listening
            self._refresh_menu()
            self._set_status("Listening…" if self._dictation_listening else "Ready")
            if self.icon:
                state = "Dictation listening started" if self._dictation_listening else "Dictation listening stopped"
                self.icon.notify(state, "TinyReadAloud")
            # Hook / unhook any-key-stops-dictation
            if self._dictation_listening:
                self._dictation_started_at = time.monotonic()
                try:
                    self._dictation_anykey_hook = keyboard.on_press(
                        self._on_anykey_stop_dictation
                    )
                except Exception:
                    pass
            else:
                self._unhook_dictation_anykey()
                # Always run grammar after dictation stops (if grammar not off)
                if self._grammar_mode != "off":
                    target_hwnd = (
                        self._status_bar._last_target_hwnd
                        if self._status_bar is not None
                        else user32.GetForegroundWindow()
                    ) or user32.GetForegroundWindow()
                    self._grammar_cancel.set()
                    t = threading.Thread(
                        target=self._run_grammar_select_all,
                        args=(target_hwnd,), daemon=True,
                    )
                    self._grammar_thread = t
                    t.start()
        except Exception as e:
            if self.icon:
                self.icon.notify(f"Dictation toggle failed: {e}", "Dictation Error")

    def _cmd_check_grammar(self, icon, item):
        threading.Thread(
            target=self._run_grammar_select_all,
            daemon=True,
        ).start()

    def _cmd_rephrase(self, icon, item):
        threading.Thread(
            target=self._run_rephrase_select_all,
            daemon=True,
        ).start()

    def _reset_hotkeys(self):
        """Re-register all hotkeys to reset the keyboard library suppress state.
        Called after any keyboard.send() sequence so the next hotkey press is
        recognised correctly."""
        for attr, hotkey, cb in (
            ("_hotkey_handle",           self._hotkey,           self._on_hotkey),
            ("_ocr_hotkey_handle",         self._ocr_hotkey,       self._on_ocr_hotkey),
            ("_dictation_hotkey_handle", self._dictation_hotkey, self._on_dictation_hotkey),
            ("_grammar_hotkey_handle",   self._grammar_hotkey,   self._on_grammar_hotkey),
            ("_rephrase_hotkey_handle",  self._rephrase_hotkey,  self._on_rephrase_hotkey),
        ):
            try:
                keyboard.remove_hotkey(getattr(self, attr))
            except Exception:
                pass
            try:
                setattr(self, attr, keyboard.add_hotkey(hotkey, cb, suppress=False))
            except Exception:
                pass
        self._register_style_hotkeys()

    def _run_grammar_select_all(self, target_hwnd=None):
        """Ctrl+A → Ctrl+C → grammar check → Ctrl+A → Ctrl+V corrected text → Ctrl+End."""
        try:
            self._run_grammar_inner(target_hwnd)
        except Exception:
            import traceback
            print(f"[Grammar] UNHANDLED EXCEPTION:\n{traceback.format_exc()}", flush=True)
            self._set_status("Grammar error.")
        finally:
            # Re-register hotkeys so state is fresh for the next press
            self._reset_hotkeys()

    def _run_grammar_inner(self, target_hwnd=None):
        self._grammar_cancel.clear()

        # Restore keyboard focus to the target window (prevents FloatingStatusBar from
        # intercepting our ctrl+a / ctrl+c / ctrl+v sends)
        if target_hwnd:
            try:
                user32.SetForegroundWindow(target_hwnd)
                time.sleep(0.1)
            except Exception:
                pass

        # Clear clipboard so we can detect when Ctrl+C actually updates it
        clipboard_clear()
        time.sleep(0.1)

        # Wait for modifier keys to be physically released before injecting
        # ctrl+a / ctrl+c, otherwise target app receives ctrl+alt+a etc.
        _wait_for_modifiers_released()

        keyboard.send("ctrl+a")
        time.sleep(0.2)
        keyboard.send("ctrl+c")

        # Poll until clipboard has content (up to 1.5s)
        text = ""
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            time.sleep(0.05)
            text = clipboard_get_text().strip()
            if text:
                break

        print(f"[Grammar] Captured {len(text)} chars from clipboard.", flush=True)

        if not text:
            self._set_status("Nothing selected.")
            if self.icon:
                self.icon.notify("Nothing to grammar-check — clipboard was empty.", "Grammar Check")
            return

        self._set_status("Checking grammar…")
        if self.icon:
            self.icon.notify(f"Checking grammar ({len(text)} chars)…", "Grammar Check")

        try:
            corrected = grammar_check_text(
                text,
                self._grammar_provider,
                api_key=self._anthropic_api_key,
                model=self._anthropic_model,
            )
        except Exception as e:
            print(f"[Grammar] API error: {e}", flush=True)
            err_str = str(e)
            if "not_found_error" in err_str or "404" in err_str:
                msg = "Model not found. Open Settings → Anthropic model and enter a valid model for your account."
            elif "authentication" in err_str.lower() or "401" in err_str:
                msg = "Invalid API key. Open Settings → Anthropic API key."
            else:
                msg = f"Grammar check failed: {e}"
            self._set_status("Grammar error.")
            if self.icon:
                self.icon.notify(msg, "Grammar Check")
            return

        print(f"[Grammar] Corrected {len(corrected)} chars.", flush=True)

        if self._grammar_cancel.is_set():
            print("[Grammar] Cancelled after API call.", flush=True)
            self._set_status("Cancelled.")
            return

        if corrected.strip() == text.strip():
            keyboard.send("ctrl+end")
            self._set_status("No changes.")
            if self.icon:
                self.icon.notify("No grammar changes suggested.", "Grammar Check")
            return

        # ctrl+a to re-select all, then overwrite clipboard and paste.
        if target_hwnd:
            try:
                user32.SetForegroundWindow(target_hwnd)
                time.sleep(0.1)
            except Exception:
                pass
        keyboard.send("ctrl+a")
        time.sleep(0.3)
        ok = clipboard_set_text(corrected)
        if not ok:
            print("[Grammar] clipboard_set_text failed — aborting paste.", flush=True)
            self._set_status("Clipboard error.")
            if self.icon:
                self.icon.notify("Could not write to clipboard. Try again.", "Grammar Check")
            return
        # Verify the write took before issuing ctrl+v
        verify = clipboard_get_text()
        print(f"[Grammar] Clipboard verify: {len(verify)} chars", flush=True)
        time.sleep(0.05)
        keyboard.send("ctrl+v")
        time.sleep(0.15)
        keyboard.send("ctrl+end")
        self._set_status("Grammar applied.")
        if self.icon:
            self.icon.notify("Grammar applied. Ready to continue.", "Grammar Check")

    def _run_rephrase_select_all(self, target_hwnd=None):
        """Ctrl+A → Ctrl+C → rephrase via AI → Ctrl+A → Ctrl+V rephrased text → Ctrl+End."""
        try:
            self._run_rephrase_inner(target_hwnd)
        except Exception:
            import traceback
            print(f"[Rephrase] UNHANDLED EXCEPTION:\n{traceback.format_exc()}", flush=True)
            self._set_status("Rephrase error.")
        finally:
            # Re-register hotkeys so state is fresh for the next press
            self._reset_hotkeys()

    def _run_rephrase_inner(self, target_hwnd=None):
        self._rephrase_cancel.clear()
        time.sleep(0.3)

        # --- Phase 1: Focus capture window ---
        fg_before = user32.GetForegroundWindow()
        target_hex = f"{target_hwnd:#010x}" if target_hwnd else "0"
        print(f"[Rephrase] Phase1 focus: current_fg={fg_before:#010x}  target={target_hex}", flush=True)
        if target_hwnd:
            ok_focus = user32.SetForegroundWindow(target_hwnd)
            time.sleep(0.15)
            fg_after = user32.GetForegroundWindow()
            print(f"[Rephrase] SetForegroundWindow({target_hex}) -> ok={ok_focus}  now_fg={fg_after:#010x}", flush=True)

        clipboard_clear()
        time.sleep(0.1)

        # Wait for modifier keys to be physically released before injecting
        # ctrl+a / ctrl+c, otherwise target app receives ctrl+alt+a etc.
        _wait_for_modifiers_released()

        fg_pre_copy = user32.GetForegroundWindow()
        print(f"[Rephrase] Before ctrl+a/ctrl+c: fg={fg_pre_copy:#010x}", flush=True)
        keyboard.send("ctrl+a")
        time.sleep(0.2)
        keyboard.send("ctrl+c")

        # Poll until clipboard has content (up to 1.5s)
        text = ""
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            time.sleep(0.05)
            text = clipboard_get_text().strip()
            if text:
                break

        print(f"[Rephrase] Captured {len(text)} chars. Text[:80]={text[:80]!r}", flush=True)

        if not text:
            self._set_status("Nothing selected.")
            if self.icon:
                self.icon.notify("Nothing to rephrase — clipboard was empty.", "Rephrase")
            return

        print(f"[Rephrase] Calling API with style={self._rephrase_style!r}  model={self._anthropic_model!r}", flush=True)
        self._set_status("Rephrasing…")
        if self.icon:
            self.icon.notify(f"Rephrasing ({len(text)} chars)…", "Rephrase")

        if self._rephrase_cancel.is_set():
            print("[Rephrase] Cancelled before API call.", flush=True)
            self._set_status("Cancelled.")
            return

        try:
            rephrased = rephrase_text(
                text,
                self._grammar_provider,
                api_key=self._anthropic_api_key,
                model=self._anthropic_model,
                style=self._rephrase_style,
            )
        except Exception as e:
            print(f"[Rephrase] API error: {e}", flush=True)
            err_str = str(e)
            if "not_found_error" in err_str or "404" in err_str:
                msg = "Model not found. Open Settings → Anthropic model and enter a valid model."
            elif "authentication" in err_str.lower() or "401" in err_str:
                msg = "Invalid API key. Open Settings → Anthropic API key."
            else:
                msg = f"Rephrase failed: {e}"
            self._set_status("Rephrase error.")
            if self.icon:
                self.icon.notify(msg, "Rephrase")
            return

        print(f"[Rephrase] API done. Input[:80]={text[:80]!r}", flush=True)
        print(f"[Rephrase] API done. Output[:200]={rephrased[:200]!r}", flush=True)
        same = rephrased.strip() == text.strip()
        print(f"[Rephrase] Same as input? {same}", flush=True)

        if self._rephrase_cancel.is_set():
            print("[Rephrase] Cancelled after API call.", flush=True)
            self._set_status("Cancelled.")
            return

        # --- Phase 2: Focus capture window again, paste result ---
        fg_pre_paste = user32.GetForegroundWindow()
        print(f"[Rephrase] Phase2 focus before paste: fg={fg_pre_paste:#010x}  target={target_hex}", flush=True)
        if target_hwnd:
            ok_focus2 = user32.SetForegroundWindow(target_hwnd)
            time.sleep(0.15)
            fg_after2 = user32.GetForegroundWindow()
            print(f"[Rephrase] SetForegroundWindow({target_hex}) -> ok={ok_focus2}  now_fg={fg_after2:#010x}", flush=True)

        keyboard.send("ctrl+a")
        time.sleep(0.3)
        ok = clipboard_set_text(rephrased)
        print(f"[Rephrase] clipboard_set_text returned {ok}", flush=True)
        if not ok:
            self._set_status("Clipboard error.")
            if self.icon:
                self.icon.notify("Could not write to clipboard. Try again.", "Rephrase")
            return
        # Verify the write took before issuing ctrl+v
        verify = clipboard_get_text()
        print(f"[Rephrase] Clipboard verify: {len(verify)} chars: {verify[:80]!r}", flush=True)
        fg_pre_v = user32.GetForegroundWindow()
        print(f"[Rephrase] fg before ctrl+v: {fg_pre_v:#010x}", flush=True)
        time.sleep(0.05)
        keyboard.send("ctrl+v")
        time.sleep(0.15)
        keyboard.send("ctrl+end")
        self._set_status("Rephrased.")
        if self.icon:
            self.icon.notify("Rephrased. Ready to continue.", "Rephrase")

    def _cmd_settings(self, icon, item):
        SettingsWindow.open(self)

    def _check_for_updates_background(self):
        """Run update check in background, notify if update found."""
        from updater import check_for_update
        info = check_for_update(__version__)
        if info and info.available:
            self._update_info = info
            if self.icon:
                self.icon.menu = self._build_menu()
                self.icon.update_menu()
                self.icon.notify(
                    f"TinyReadAloud v{info.tag} is available!",
                    "Update Available")

    def _cmd_check_updates(self, icon, item):
        """Manual update check from tray menu."""
        threading.Thread(target=self._manual_update_check, daemon=True).start()

    def _manual_update_check(self):
        from updater import check_for_update
        info = check_for_update(__version__)
        if info and info.available:
            self._update_info = info
            if self.icon:
                self.icon.menu = self._build_menu()
                self.icon.update_menu()
                self.icon.notify(
                    f"TinyReadAloud v{info.tag} is available!",
                    "Update Available")
        else:
            if self.icon:
                self.icon.notify("You are running the latest version.",
                                 "No Update Available")

    def _cmd_download_update(self, icon, item):
        """Download and run the new installer."""
        if not self._update_info or not self._update_info.download_url:
            return
        threading.Thread(target=self._do_download_update, daemon=True).start()

    def _do_download_update(self):
        from updater import download_and_run_installer
        if self.icon:
            self.icon.notify("Downloading update...", "TinyReadAloud")
        success = download_and_run_installer(self._update_info.download_url)
        if success:
            self._cmd_exit(self.icon, None)
        else:
            if self.icon:
                self.icon.notify("Download failed. Try again later.",
                                 "Update Error")

    def _update_hotkey(self, new_hotkey):
        """Re-register the global hotkey and update tooltip."""
        if self._hotkey_handle is not None:
            try:
                keyboard.remove_hotkey(self._hotkey_handle)
            except (KeyError, ValueError):
                pass
            self._hotkey_handle = None
        self._hotkey = new_hotkey
        if new_hotkey:
            self._hotkey_handle = keyboard.add_hotkey(self._hotkey, self._on_hotkey, suppress=False)
        if self.icon:
            self.icon.title = self._tray_title()

    def _update_ocr_hotkey(self, new_hotkey):
        if self._ocr_hotkey_handle is not None:
            try:
                keyboard.remove_hotkey(self._ocr_hotkey_handle)
            except (KeyError, ValueError):
                pass
            self._ocr_hotkey_handle = None
        self._ocr_hotkey = new_hotkey
        if new_hotkey:
            self._ocr_hotkey_handle = keyboard.add_hotkey(
                self._ocr_hotkey, self._on_ocr_hotkey, suppress=False
            )
        if self.icon:
            self.icon.title = self._tray_title()

    def _tray_title(self):
        return (
            f"TinyReadAloud v{__version__}  "
            f"[Read: {self._hotkey} | OCR: {self._ocr_hotkey} | "
            f"Dictation: {self._dictation_hotkey} | Grammar: {self._grammar_hotkey} | "
            f"Rephrase: {self._rephrase_hotkey}]"
        )

    def _update_dictation_hotkey(self, new_hotkey):
        if self._dictation_hotkey_handle is not None:
            try:
                keyboard.remove_hotkey(self._dictation_hotkey_handle)
            except (KeyError, ValueError):
                pass
            self._dictation_hotkey_handle = None
        self._dictation_hotkey = new_hotkey
        if new_hotkey:
            self._dictation_hotkey_handle = keyboard.add_hotkey(
                self._dictation_hotkey, self._on_dictation_hotkey, suppress=False
            )
        if self.icon:
            self.icon.title = f"TinyReadAloud v{__version__}  [Read: {self._hotkey} | Dictation: {self._dictation_hotkey} | Grammar: {self._grammar_hotkey} | Rephrase: {self._rephrase_hotkey}]"

    def _update_grammar_hotkey(self, new_hotkey):
        if self._grammar_hotkey_handle is not None:
            try:
                keyboard.remove_hotkey(self._grammar_hotkey_handle)
            except (KeyError, ValueError):
                pass
            self._grammar_hotkey_handle = None
        self._grammar_hotkey = new_hotkey
        if new_hotkey:
            self._grammar_hotkey_handle = keyboard.add_hotkey(
                self._grammar_hotkey, self._on_grammar_hotkey, suppress=False
            )
        if self.icon:
            self.icon.title = f"TinyReadAloud v{__version__}  [Read: {self._hotkey} | Dictation: {self._dictation_hotkey} | Grammar: {self._grammar_hotkey} | Rephrase: {self._rephrase_hotkey}]"

    def _update_rephrase_hotkey(self, new_hotkey):
        if self._rephrase_hotkey_handle is not None:
            try:
                keyboard.remove_hotkey(self._rephrase_hotkey_handle)
            except (KeyError, ValueError):
                pass
            self._rephrase_hotkey_handle = None
        self._rephrase_hotkey = new_hotkey
        if new_hotkey:
            self._rephrase_hotkey_handle = keyboard.add_hotkey(
                self._rephrase_hotkey, self._on_rephrase_hotkey, suppress=False
            )
        if self.icon:
            self.icon.title = f"TinyReadAloud v{__version__}  [Read: {self._hotkey} | Dictation: {self._dictation_hotkey} | Grammar: {self._grammar_hotkey} | Rephrase: {self._rephrase_hotkey}]"

    def _refresh_menu(self):
        if self.icon:
            self.icon.menu = self._build_menu()
            self.icon.update_menu()

    def _cmd_exit(self, icon, item):
        if self._hotkey_handle is not None:
            keyboard.remove_hotkey(self._hotkey_handle)
        if self._dictation_hotkey_handle is not None:
            keyboard.remove_hotkey(self._dictation_hotkey_handle)
        if self._grammar_hotkey_handle is not None:
            keyboard.remove_hotkey(self._grammar_hotkey_handle)
        if self._rephrase_hotkey_handle is not None:
            keyboard.remove_hotkey(self._rephrase_hotkey_handle)
        for h in self._style_hotkey_handles.values():
            try:
                keyboard.remove_hotkey(h)
            except Exception:
                pass
        if self._recall_style_hotkey_handle is not None:
            try:
                keyboard.remove_hotkey(self._recall_style_hotkey_handle)
            except Exception:
                pass
        self.tts.quit()
        icon.stop()


# ── Entry Point ──────────────────────────────────────────────────────────────

def main():
    if not ensure_models():
        print("Cannot start without model files.", file=sys.stderr)
        sys.exit(1)

    print("Loading Kokoro TTS model...")
    app = TinyReadAloud()

    def _sigint_handler(sig, frame):
        if app.icon:
            app.icon.stop()

    signal.signal(signal.SIGINT, _sigint_handler)
    app.run()


if __name__ == "__main__":
    main()
