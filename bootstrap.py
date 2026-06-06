"""Runtime bootstrap — must run before onnxruntime / CUDA imports."""

from __future__ import annotations

import ctypes
import os
import sys

_configured = False


def configure_runtime():
    """Set DPI awareness and NVIDIA DLL paths once per process."""
    global _configured
    if _configured:
        return
    _configured = True

    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

    try:
        import nvidia
        nv_root = os.path.dirname(
            nvidia.__path__[0]
            if hasattr(nvidia.__path__, "__iter__")
            else nvidia.__path__
        )
        for subpkg in ("cublas", "cuda_runtime", "cudnn", "cufft", "nvjitlink"):
            bin_dir = os.path.join(nv_root, "nvidia", subpkg, "bin")
            if os.path.isdir(bin_dir):
                os.add_dll_directory(bin_dir)
                os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
    except ImportError:
        pass
