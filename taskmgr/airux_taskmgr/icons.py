"""Find an app icon file by its theme name (Breeze, then hicolor, then pixmaps), for the web UIs."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

THEMES = ("breeze-dark", "breeze", "hicolor", "Adwaita")
SIZES = ("scalable", "64x64", "48x48", "128x128", "256x256", "32x32", "64", "48", "32")
CTYPES = {".svg": "image/svg+xml", ".png": "image/png"}


@lru_cache(maxsize=512)
def find(name: str, root: str = "/usr/share") -> Path | None:
    if not name:
        return None
    p = Path(name)
    if p.is_absolute():
        return p if p.suffix in CTYPES and p.is_file() and str(p).startswith(("/usr/share/", "/opt/")) else None
    if not re.fullmatch(r"[A-Za-z0-9._+-]{1,120}", name):
        return None
    base = Path(root) / "icons"
    for theme in THEMES:
        for size in SIZES:
            for cat in ("apps", "categories", "places", "devices", "mimetypes"):
                for ext in (".svg", ".png"):
                    # hicolor: <size>/<category>/, Breeze: <category>/<size>/
                    for f in (base / theme / size / cat / f"{name}{ext}", base / theme / cat / size / f"{name}{ext}"):
                        if f.is_file():
                            return f
    for ext in (".svg", ".png"):
        f = Path(root) / "pixmaps" / f"{name}{ext}"
        if f.is_file():
            return f
    return None
