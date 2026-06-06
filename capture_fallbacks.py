"""Fallback text capture: UI Automation, region OCR, and OCR text reflow."""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes
import io
import re
import sys

user32 = ctypes.windll.user32

user32.GetClientRect.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.RECT)]
user32.GetClientRect.restype = ctypes.wintypes.BOOL
user32.ClientToScreen.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.POINT)]
user32.ClientToScreen.restype = ctypes.wintypes.BOOL
user32.IsWindow.argtypes = [ctypes.wintypes.HWND]
user32.IsWindow.restype = ctypes.wintypes.BOOL


def reflow_ocr_text(raw: str) -> str:
    """Join OCR line breaks into readable sentences and fix hyphenation."""
    if not raw:
        return ""

    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = re.split(r"\n\s*\n", text)
    fixed_paragraphs = []

    for para in paragraphs:
        lines = [ln.strip() for ln in para.split("\n") if ln.strip()]
        if not lines:
            continue
        merged = lines[0]
        for nxt in lines[1:]:
            merged = _join_ocr_lines(merged, nxt)
        fixed_paragraphs.append(re.sub(r" +", " ", merged).strip())

    return "\n\n".join(fixed_paragraphs)


def _join_ocr_lines(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left

    # resolu- + tion  ->  resolution
    if left.endswith("-") and right and right[0].islower():
        return left[:-1] + right

    # End-of-sentence or clause: keep as separate sentence start.
    if left[-1] in ".!?":
        return left + " " + right

    # Continuation of same sentence (wrap): lowercase start or comma/colon end.
    if right[0].islower() or left[-1] in ",:;":
        return left + " " + right

    # Digit continuation (e.g. page numbers split across lines).
    if left[-1].isdigit() and right[0].isdigit():
        return left + right

    return left + " " + right


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

        try:
            vp = focused.GetValuePattern()
            if vp and not vp.IsReadOnly:
                text = (vp.Value or "").strip()
                if text:
                    print(f"[Capture] UIA value ok len={len(text)}", flush=True)
                    return text
        except Exception:
            pass

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


def _screenshot_bbox(left, top, width, height):
    try:
        import mss
        from PIL import Image
    except ImportError:
        print("[Capture] mss/Pillow not available — skip OCR screenshot", flush=True)
        return None

    if width < 8 or height < 8:
        return None

    left, top, width, height = int(left), int(top), int(width), int(height)

    try:
        with mss.MSS() as sct:
            virtual = sct.monitors[0]
            right = left + width
            bottom = top + height
            vright = virtual["left"] + virtual["width"]
            vbottom = virtual["top"] + virtual["height"]
            left = max(virtual["left"], left)
            top = max(virtual["top"], top)
            right = min(vright, right)
            bottom = min(vbottom, bottom)
            width = right - left
            height = bottom - top
            if width < 8 or height < 8:
                print(
                    f"[Capture] region outside desktop: "
                    f"{left},{top} {width}x{height}",
                    flush=True,
                )
                return None
            print(
                f"[Capture] screenshot left={left} top={top} w={width} h={height}",
                flush=True,
            )
            shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
            if shot.width < 1 or shot.height < 1:
                return None
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    except Exception as exc:
        print(f"[Capture] screenshot failed: {exc}", flush=True)
        return None


async def _ocr_pil_async(pil_image) -> str:
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    bio = io.BytesIO()
    pil_image.convert("RGB").save(bio, format="PNG")
    png_bytes = bio.getvalue()

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(png_bytes)
    await writer.store_async()
    await writer.flush_async()
    stream.seek(0)

    decoder = await BitmapDecoder.create_async(stream)
    software_bitmap = await decoder.get_software_bitmap_async()

    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        try:
            from winrt.windows.globalization import Language
            engine = OcrEngine.try_create_from_language(Language("en-US"))
        except Exception:
            engine = None
    if engine is None:
        raise RuntimeError("Windows OCR engine unavailable (install a language pack)")

    result = await engine.recognize_async(software_bitmap)
    return (result.text or "").strip()


def ocr_pil_image(pil_image) -> str:
    if sys.platform != "win32":
        return ""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(_ocr_pil_async(pil_image))
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def capture_ocr_region(bbox) -> str:
    """OCR a screen rectangle (left, top, width, height). Returns reflowed text."""
    if not bbox:
        return ""

    try:
        from winrt.windows.media.ocr import OcrEngine  # noqa: F401
    except ImportError:
        print("[Capture] winrt OCR packages not installed — skip OCR", flush=True)
        return ""

    left, top, width, height = bbox
    image = _screenshot_bbox(left, top, width, height)
    if image is None:
        return ""

    # Upscale small captures — OCR accuracy improves with pixel density.
    try:
        from PIL import Image
        if image.width < 240 or image.height < 80:
            scale = 2
            image = image.resize(
                (image.width * scale, image.height * scale),
                Image.Resampling.LANCZOS,
            )
    except Exception:
        pass

    try:
        raw = ocr_pil_image(image)
        text = reflow_ocr_text(raw)
        if text:
            print(f"[Capture] region OCR ok len={len(text)}", flush=True)
        else:
            print("[Capture] region OCR returned no text", flush=True)
        return text
    except Exception as exc:
        print(f"[Capture] region OCR failed: {exc}", flush=True)
        return ""
