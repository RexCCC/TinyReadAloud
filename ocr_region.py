"""Fullscreen dim overlay to drag-select a screen region for OCR."""

from __future__ import annotations

import ctypes
import threading
import tkinter as tk

user32 = ctypes.windll.user32

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


def get_virtual_screen_bounds():
    """Return (left, top, width, height) spanning all monitors."""
    try:
        import mss
        with mss.MSS() as sct:
            mon = sct.monitors[0]
            return mon["left"], mon["top"], mon["width"], mon["height"]
    except Exception:
        pass
    left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return left, top, width, height


class RegionSelectOverlay:
    """Grey out all monitors; user drags a rectangle; callback receives bbox or None."""

    MIN_SIZE = 24
    DIM = "#000000"
    BORDER = "#a78bfa"
    FILL = "#ffffff"
    HINT = "Drag to select text on any monitor.  Esc = cancel"

    @classmethod
    def pick(cls, on_result, parent_root=None):
        """Start region pick. Prefer parent_root (toolbar Tk) to avoid a second Tk()."""
        if parent_root is not None:
            try:
                parent_root.after(0, lambda: cls._run_toplevel(parent_root, on_result))
                return
            except Exception:
                pass
        threading.Thread(
            target=cls._run_standalone, args=(on_result,), daemon=True
        ).start()

    @classmethod
    def _run_toplevel(cls, parent, on_result):
        vx, vy, vw, vh = get_virtual_screen_bounds()
        win = tk.Toplevel(parent)
        win.overrideredirect(True)
        win.geometry(f"{vw}x{vh}+{vx}+{vy}")
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.45)
        win.configure(bg=cls.DIM, cursor="crosshair")
        try:
            win.grab_set_global()
        except Exception:
            try:
                win.grab_set()
            except Exception:
                pass
        cls._run_loop(win, on_result, vx, vy, modal=True)

    @classmethod
    def _run_standalone(cls, on_result):
        vx, vy, vw, vh = get_virtual_screen_bounds()
        win = tk.Tk()
        win.overrideredirect(True)
        win.geometry(f"{vw}x{vh}+{vx}+{vy}")
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.45)
        win.configure(bg=cls.DIM, cursor="crosshair")
        cls._run_loop(win, on_result, vx, vy, modal=False)

    @classmethod
    def _run_loop(cls, win, on_result, offset_x, offset_y, modal=False):
        vx, vy = offset_x, offset_y
        _, _, vw, vh = get_virtual_screen_bounds()

        canvas = tk.Canvas(
            win, width=vw, height=vh, bg=cls.DIM, highlightthickness=0,
        )
        canvas.pack(fill="both", expand=True)

        hint = tk.Label(
            win, text=cls.HINT, bg="#1a1a2e", fg="#e2e8f0",
            font=("Segoe UI", 11), padx=12, pady=6,
        )
        hint.place(relx=0.5, y=24, anchor="n")

        state = {"x0": 0, "y0": 0, "rect": None, "fill": None}
        result = {"bbox": None}

        def _screen_to_canvas(x_root, y_root):
            return x_root - vx, y_root - vy

        def _clear():
            if state["rect"]:
                canvas.delete(state["rect"])
                state["rect"] = None
            if state["fill"]:
                canvas.delete(state["fill"])
                state["fill"] = None

        def _on_press(event):
            state["x0"], state["y0"] = event.x_root, event.y_root
            _clear()

        def _on_motion(event):
            _clear()
            x0, y0 = state["x0"], state["y0"]
            x1, y1 = event.x_root, event.y_root
            left, top = min(x0, x1), min(y0, y1)
            right, bottom = max(x0, x1), max(y0, y1)
            cl, ct = _screen_to_canvas(left, top)
            cr, cb = _screen_to_canvas(right, bottom)
            state["fill"] = canvas.create_rectangle(
                cl, ct, cr, cb, outline="", fill=cls.FILL, stipple="gray25",
            )
            state["rect"] = canvas.create_rectangle(
                cl, ct, cr, cb, outline=cls.BORDER, width=2,
            )

        def _finish(bbox):
            result["bbox"] = bbox
            try:
                win.grab_release()
            except Exception:
                pass
            try:
                win.destroy()
            except Exception:
                pass

        def _on_release(event):
            x0, y0 = state["x0"], state["y0"]
            x1, y1 = event.x_root, event.y_root
            left = min(x0, x1)
            top = min(y0, y1)
            width = abs(x1 - x0)
            height = abs(y1 - y0)
            if width >= cls.MIN_SIZE and height >= cls.MIN_SIZE:
                _finish((int(left), int(top), int(width), int(height)))
            else:
                _finish(None)

        def _on_escape(_event=None):
            _finish(None)

        canvas.bind("<ButtonPress-1>", _on_press)
        canvas.bind("<B1-Motion>", _on_motion)
        canvas.bind("<ButtonRelease-1>", _on_release)
        win.bind("<Escape>", _on_escape)

        try:
            if modal and isinstance(win, tk.Toplevel):
                win.wait_window()
            else:
                win.mainloop()
        finally:
            pass

        try:
            on_result(result["bbox"])
        except Exception as exc:
            print(f"[OCR] region callback failed: {exc}", flush=True)
            import traceback
            traceback.print_exc()
