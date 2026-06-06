"""Fullscreen dim overlay to drag-select a screen region for OCR."""

from __future__ import annotations

import threading
import tkinter as tk


class RegionSelectOverlay:
    """Grey out the screen; user drags a rectangle; callback receives bbox or None."""

    MIN_SIZE = 24
    DIM = "#000000"
    BORDER = "#a78bfa"
    FILL = "#ffffff"
    HINT = "Drag to select text.  Esc = cancel"

    @classmethod
    def pick(cls, on_result):
        """Run selector on a background thread. on_result(bbox|None), bbox=(l,t,w,h)."""
        threading.Thread(target=cls._run, args=(on_result,), daemon=True).start()

    @classmethod
    def _run(cls, on_result):
        result = {"bbox": None}

        root = tk.Tk()
        root.withdraw()
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        root.destroy()

        win = tk.Tk()
        win.overrideredirect(True)
        win.geometry(f"{sw}x{sh}+0+0")
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.45)
        win.configure(bg=cls.DIM, cursor="crosshair")

        canvas = tk.Canvas(win, width=sw, height=sh, bg=cls.DIM, highlightthickness=0)
        canvas.pack(fill="both", expand=True)

        hint = tk.Label(
            win, text=cls.HINT, bg="#1a1a2e", fg="#e2e8f0",
            font=("Segoe UI", 11), padx=12, pady=6,
        )
        hint.place(relx=0.5, y=24, anchor="n")

        state = {"x0": 0, "y0": 0, "rect": None, "fill": None}

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
            state["fill"] = canvas.create_rectangle(
                left, top, right, bottom,
                outline="", fill=cls.FILL, stipple="gray25",
            )
            state["rect"] = canvas.create_rectangle(
                left, top, right, bottom,
                outline=cls.BORDER, width=2,
            )

        def _finish(bbox):
            result["bbox"] = bbox
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
                _finish((left, top, width, height))
            else:
                _finish(None)

        def _on_escape(_event=None):
            _finish(None)

        canvas.bind("<ButtonPress-1>", _on_press)
        canvas.bind("<B1-Motion>", _on_motion)
        canvas.bind("<ButtonRelease-1>", _on_release)
        win.bind("<Escape>", _on_escape)

        try:
            win.mainloop()
        finally:
            pass

        try:
            on_result(result["bbox"])
        except Exception as exc:
            print(f"[OCR] region callback failed: {exc}", flush=True)
