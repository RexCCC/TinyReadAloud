"""Systematic tests for Rex TinyReadAloud fork features."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app as app_mod
import capture_fallbacks as cap_mod
import ocr_region as ocr_mod


class TestSpeedHelpers(unittest.TestCase):
    def test_format_speed_numeric(self):
        self.assertEqual(app_mod.format_speed(1.0), "1.0")
        self.assertEqual(app_mod.format_speed(1.5), "1.5")

    def test_parse_speed_accepts_numeric_strings(self):
        self.assertEqual(app_mod.parse_speed("1.2"), 1.2)
        self.assertEqual(app_mod.parse_speed("1.0x"), 1.0)
        self.assertEqual(app_mod.parse_speed("1.5×"), 1.5)

    def test_parse_speed_invalid_returns_default(self):
        self.assertEqual(app_mod.parse_speed("Fast"), app_mod.DEFAULT_SPEED)

    def test_clamp_speed_bounds(self):
        self.assertEqual(app_mod.clamp_speed(0.1), 0.5)
        self.assertEqual(app_mod.clamp_speed(9.9), 3.0)

    def test_preset_index_all_presets(self):
        for i, preset in enumerate(app_mod.SPEED_PRESETS):
            self.assertEqual(app_mod.preset_index(preset), i)

    def test_preset_step_up_down(self):
        idx = app_mod.preset_index(1.0)
        self.assertEqual(app_mod.SPEED_PRESETS[idx + 1], 1.1)
        self.assertEqual(app_mod.SPEED_PRESETS[idx - 1], 0.9)


class TestOcrReflow(unittest.TestCase):
    def test_hyphenation_across_lines(self):
        out = cap_mod.reflow_ocr_text("resolu-\ntion of the problem")
        self.assertIn("resolution", out)
        self.assertNotIn("resolu-", out)

    def test_sentence_wrap_lowercase_continuation(self):
        out = cap_mod.reflow_ocr_text("The quick brown fox\njumps over the lazy dog.")
        self.assertIn("fox jumps", out)

    def test_paragraph_break_preserved(self):
        out = cap_mod.reflow_ocr_text("First paragraph end.\n\nSecond paragraph start.")
        self.assertIn("\n\n", out)

    def test_join_hyphen_rule_direct(self):
        self.assertEqual(cap_mod._join_ocr_lines("resolu-", "tion"), "resolution")


class TestMarkdownStrip(unittest.TestCase):
    def test_bold_removed(self):
        out = app_mod.strip_markdown_for_speech("This is **important** text.")
        self.assertEqual(out, "This is important text.")

    def test_headers_removed(self):
        out = app_mod.strip_markdown_for_speech("# Title\n\n## Section\n\nBody.")
        self.assertEqual(out, "Title\n\nSection\n\nBody.")

    def test_mixed_markdown(self):
        raw = "## Intro\n\nRead **this** and [a link](https://x.com)."
        out = app_mod.strip_markdown_for_speech(raw)
        self.assertIn("Intro", out)
        self.assertNotIn("**", out)
        self.assertNotIn("##", out)
        self.assertIn("this", out)
        self.assertIn("a link", out)
        self.assertNotIn("https", out)


class TestSplitSentences(unittest.TestCase):
    def test_basic_sentences(self):
        parts = app_mod.split_sentences("Hello world. How are you? Fine!")
        self.assertEqual(len(parts), 3)

    def test_ocr_reflowed_text(self):
        text = cap_mod.reflow_ocr_text("resolu-\ntion works well. Another sentence here.")
        parts = app_mod.split_sentences(text)
        self.assertGreaterEqual(len(parts), 1)


class TestAppIcon(unittest.TestCase):
    def test_tray_icon_sizes(self):
        for sz in (16, 64, 256):
            idle = app_mod.create_tray_icon(size=sz, speaking=False)
            talk = app_mod.create_tray_icon(size=sz, speaking=True)
            self.assertEqual(idle.size, (sz, sz))
            self.assertEqual(talk.size, (sz, sz))
            self.assertEqual(idle.mode, "RGBA")


class TestConfigDefaults(unittest.TestCase):
    def test_defaults_include_ocr_hotkey(self):
        with mock.patch.object(app_mod.os.path, "exists", return_value=False):
            cfg = app_mod.load_config()
        self.assertEqual(cfg["ocr_hotkey"], app_mod.OCR_HOTKEY)

    def test_defaults_include_toolbar_settings(self):
        with mock.patch.object(app_mod.os.path, "exists", return_value=False):
            cfg = app_mod.load_config()
        self.assertEqual(cfg["toolbar_alpha"], app_mod.DEFAULT_TOOLBAR_ALPHA)
        self.assertTrue(cfg["toolbar_visible"])
        self.assertFalse(cfg["stop_reading_on_keypress"])
        self.assertFalse(cfg["ocr_copy_only"])

    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with mock.patch.object(app_mod, "CONFIG_PATH", path):
                cfg = app_mod.load_config()
                cfg["speed"] = 1.3
                app_mod.save_config(cfg)
                loaded = app_mod.load_config()
                self.assertEqual(loaded["speed"], 1.3)


class TestVirtualScreenBounds(unittest.TestCase):
    def test_bounds_sane(self):
        left, top, width, height = ocr_mod.get_virtual_screen_bounds()
        self.assertGreater(width, 800)
        self.assertGreater(height, 600)

    def test_mss_monitor_count(self):
        import mss
        with mss.MSS() as sct:
            self.assertGreaterEqual(len(sct.monitors) - 1, 1)

    def test_screen_to_canvas_offset(self):
        vx, vy, _, _ = ocr_mod.get_virtual_screen_bounds()
        self.assertEqual((vx + 50) - vx, 50)


class TestScreenshotCapture(unittest.TestCase):
    def test_primary_monitor_grab_non_empty(self):
        import mss
        with mss.MSS() as sct:
            mon = sct.monitors[1]
        img = cap_mod._screenshot_bbox(mon["left"] + 10, mon["top"] + 10, 120, 80)
        self.assertIsNotNone(img)
        self.assertGreaterEqual(img.width, 120)

    def test_tiny_region_rejected(self):
        self.assertIsNone(cap_mod._screenshot_bbox(0, 0, 4, 4))

    def test_clip_outside_virtual_desktop(self):
        import mss
        with mss.MSS() as sct:
            v = sct.monitors[0]
        self.assertIsNone(cap_mod._screenshot_bbox(
            v["left"] + v["width"] + 5000, v["top"] + 5000, 100, 100,
        ))


class TestWindowsOcr(unittest.TestCase):
    def _make_text_image(self, text="Hello OCR Test 123"):
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (640, 120), "white")
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("arial.ttf", 36)
        except Exception:
            font = ImageFont.load_default()
        draw.text((20, 40), text, fill="black", font=font)
        return img

    @unittest.skipUnless(sys.platform == "win32", "Windows OCR only")
    def test_ocr_engine_available(self):
        from winrt.windows.media.ocr import OcrEngine
        engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            from winrt.windows.globalization import Language
            engine = OcrEngine.try_create_from_language(Language("en-US"))
        self.assertIsNotNone(engine)

    @unittest.skipUnless(sys.platform == "win32", "Windows OCR only")
    def test_ocr_synthetic_image(self):
        text = cap_mod.ocr_pil_image(self._make_text_image())
        self.assertTrue(text)
        self.assertIn("OCR", text.upper())

    @unittest.skipUnless(sys.platform == "win32", "Windows OCR only")
    def test_capture_ocr_region_on_screen_strip(self):
        import mss
        with mss.MSS() as sct:
            mon = sct.monitors[1]
        bbox = (mon["left"] + 5, mon["top"] + 5, 300, 120)
        result = cap_mod.capture_ocr_region(bbox)
        self.assertIsInstance(result, cap_mod.OcrCaptureResult)


class TestReliability(unittest.TestCase):
    def test_health_check_runs(self):
        import reliability as rel
        report = rel.run_health_check(include_clipboard=False)
        self.assertIsInstance(report.checks, list)
        self.assertGreater(len(report.checks), 0)

    def test_ocr_result_dataclass(self):
        ok = cap_mod.OcrCaptureResult("hello", attempts=1)
        self.assertTrue(ok.ok)
        bad = cap_mod.OcrCaptureResult("", error="empty")
        self.assertFalse(bad.ok)


class TestDependencies(unittest.TestCase):
    def test_required_packages(self):
        for mod in ("mss", "uiautomation", "keyboard", "PIL", "pystray"):
            __import__(mod)


class TestOcrRegionPick(unittest.TestCase):
    def test_release_without_press_is_ignored(self):
        self.assertIsNone(ocr_mod.RegionSelectOverlay.bbox_from_drag(0, 0, 800, 600, armed=False))

    def test_valid_drag_returns_bbox(self):
        bbox = ocr_mod.RegionSelectOverlay.bbox_from_drag(100, 100, 400, 300, armed=True)
        self.assertEqual(bbox, (100, 100, 300, 200))

    def test_tiny_drag_returns_none(self):
        self.assertIsNone(ocr_mod.RegionSelectOverlay.bbox_from_drag(0, 0, 10, 10, armed=True))


class TestToolbarGeometry(unittest.TestCase):
    def test_prepare_restore_keeps_width(self):
        import tkinter as tk
        from app import FloatingStatusBar

        done = threading.Event()
        ok = {"v": False, "w": 0}

        def run():
            root = tk.Tk()
            root.withdraw()
            bar = FloatingStatusBar.__new__(FloatingStatusBar)
            bar._root = root
            bar.W = FloatingStatusBar.W
            bar.H = FloatingStatusBar.H
            bar.MIN_W = FloatingStatusBar.MIN_W
            root.geometry(f"{bar.W}x{bar.H}+100+100")
            root.update_idletasks()
            bar.prepare_for_ocr_overlay()
            bar.restore_after_ocr_overlay()
            geo = root.geometry()
            w = int(geo.split("x")[0])
            ok["w"] = w
            ok["v"] = w >= bar.MIN_W
            root.destroy()
            done.set()

        threading.Thread(target=run, daemon=True).start()
        self.assertTrue(done.wait(10))
        self.assertTrue(ok["v"], f"toolbar width too small after restore: {ok['w']}")

    def test_prepare_restore_alpha(self):
        import tkinter as tk
        done = threading.Event()
        ok = {"v": False}

        def run():
            root = tk.Tk()
            root.withdraw()
            bar = app_mod.FloatingStatusBar.__new__(app_mod.FloatingStatusBar)
            bar._root = root
            bar.W = app_mod.FloatingStatusBar.W
            bar.H = app_mod.FloatingStatusBar.H
            bar._rest_alpha = 0.5
            bar._hover_alpha = 1.0
            bar._visible = True
            bar._ocr_hidden = False
            bar._hovering = False
            root.geometry(f"{bar.W}x{bar.H}+100+100")
            root.attributes("-alpha", 0.5)
            root.update_idletasks()
            bar.prepare_for_ocr_overlay()
            bar.restore_after_ocr_overlay()
            ok["v"] = (
                root.geometry().startswith(f"{bar.W}x{bar.H}+")
                and abs(float(root.attributes("-alpha")) - 0.5) < 0.01
            )
            root.destroy()
            done.set()

        threading.Thread(target=run, daemon=True).start()
        self.assertTrue(done.wait(10))
        self.assertTrue(ok["v"])

    def test_clamp_toolbar_alpha_default(self):
        self.assertEqual(app_mod.clamp_toolbar_alpha(0.5), 0.5)
        self.assertEqual(app_mod.clamp_toolbar_alpha("bad"), app_mod.DEFAULT_TOOLBAR_ALPHA)


class TestClipboardHelpers(unittest.TestCase):
    def test_clipboard_roundtrip(self):
        self.assertTrue(app_mod.clipboard_set_text("TinyReadAloud test clip"))
        self.assertEqual(app_mod.clipboard_get_text(), "TinyReadAloud test clip")


class TestVoiceHelpers(unittest.TestCase):
    def test_voice_display_name(self):
        self.assertIn("Heart", app_mod.voice_display_name("af_heart"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
