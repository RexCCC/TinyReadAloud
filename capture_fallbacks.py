"""Fallback text capture when clipboard copy fails (UI Automation + Windows OCR)."""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes
import io
import sys

user32 = ctypes.windll.user32

user32.GetClientRect.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.RECT)]
user32.GetClientRect.restype = ctypes.wintypes.BOOL
user32.ClientToScreen.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.POINT)]
user32.ClientToScreen.restype = ctypes.wintypes.BOOL
user32.IsWindow.argtypes = [ctypes.wintypes.HWND]
user32.IsWindow.restype = ctypes.wintypes.BOOL


def _window_client_bbox(hwnd):
    """Return (left, top, width, height) of hwnd's client area in screen coords."""
    if not hwnd or not user32.IsWindow(hwnd):
        return None
    rc = ctypes.wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rc)):
        return None
    width = rc.right - rc.left
    height = rc.bottom - rc.top
    if width < 40 or height < 40:
        return None
    pt = ctypes.wintypes.POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
        return None
    return pt.x, pt.y, width, height


def capture_via_accessibility(target_hwnd=None) -> str:
    """Read selected or focused text via UI Automation (no clipboard)."""
    try:
        import uiautomation as auto
    except ImportError:
        print("[Capture] uiautomation not installed — skip UIA", flush=True)
        return ""

    try:
        focused = None
        if target_hwnd and user32.IsWindow(target_hwnd):
            root = auto.ControlFromHandle(target_hwnd)
            if root:
                try:
                    focused = root.GetFocusControl()
                except Exception:
                    focused = None
        if focused is None:
            focused = auto.GetFocusedControl()
        if focused is None:
            return ""

        # Selected text in editable controls
        try:
            tp = focused.GetTextPattern()
            if tp:
                ranges = tp.GetSelection()
                if ranges:
                    parts = []
                    for r in ranges:
                        try:
                            parts.append(r.GetText(-1))
                        except Exception:
                            pass
                    text = "".join(parts).strip()
                    if text:
                        print(f"[Capture] UIA selection ok len={len(text)}", flush=True)
                        return text
        except Exception:
            pass

        # Whole value (single-line fields)
        try:
            vp = focused.GetValuePattern()
            if vp and not vp.IsReadOnly:
                text = (vp.Value or "").strip()
                if text:
                    print(f"[Capture] UIA value ok len={len(text)}", flush=True)
                    return text
        except Exception:
            pass

        # Document pattern (some viewers / browsers)
        try:
            tp = focused.GetTextPattern()
            if tp and tp.DocumentRange:
                text = (tp.DocumentRange.GetText(-1) or "").strip()
                if text:
                    print(f"[Capture] UIA document ok len={len(text)}", flush=True)
                    return text
        except Exception:
            pass

        name = (focused.Name or "").strip()
        if len(name) > 20:
            print(f"[Capture] UIA name ok len={len(name)}", flush=True)
            return name
    except Exception as exc:
        print(f"[Capture] UIA failed: {exc}", flush=True)
    return ""


def _screenshot_client_area(hwnd):
    """Grab a PIL RGB image of the window client area."""
    bbox = _window_client_bbox(hwnd)
    if not bbox:
        return None
    left, top, width, height = bbox
    try:
        import mss
        from PIL import Image
    except ImportError:
        print("[Capture] mss/Pillow not available — skip OCR screenshot", flush=True)
        return None

    try:
        with mss.mss() as sct:
            shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    except Exception as exc:
        print(f"[Capture] screenshot failed: {exc}", flush=True)
        return None


async def _ocr_pil_async(pil_image) -> str:
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    bio = io.BytesIO()
    pil_image.convert("RGBA").save(bio, format="PNG")
    png_bytes = bio.getvalue()

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write(png_bytes)
    await writer.store_async()
    await writer.flush_async()
    stream.seek(0)

    decoder = await BitmapDecoder.create_async(stream)
    software_bitmap = await decoder.get_software_bitmap_async()

    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError("Windows OCR engine unavailable for profile languages")

    result = await engine.recognize_async(software_bitmap)
    return (result.text or "").strip()


def _ocr_pil_image(pil_image) -> str:
    if sys.platform != "win32":
        return ""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(_ocr_pil_async(pil_image))
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def capture_via_ocr(target_hwnd=None) -> str:
    """OCR the target window client area when copy/UIA fail."""
    if not target_hwnd or not user32.IsWindow(target_hwnd):
        target_hwnd = user32.GetForegroundWindow()
    if not target_hwnd:
        return ""

    try:
        from winrt.windows.media.ocr import OcrEngine  # noqa: F401
    except ImportError:
        print("[Capture] winrt OCR packages not installed — skip OCR", flush=True)
        return ""

    image = _screenshot_client_area(target_hwnd)
    if image is None:
        return ""

    try:
        text = _ocr_pil_image(image)
        if text:
            print(f"[Capture] OCR ok len={len(text)}", flush=True)
        else:
            print("[Capture] OCR returned no text", flush=True)
        return text
    except Exception as exc:
        print(f"[Capture] OCR failed: {exc}", flush=True)
        return ""