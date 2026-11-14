"""Desktop notifications through the freedesktop Notifications D-Bus service (notify-send).

Always bounded by a timeout so a missing or stuck notification daemon cannot hang AIRA.
"""
from __future__ import annotations

import shutil
import subprocess


def notify(title: str, body: str, urgency: str = "normal", timeout: float = 15) -> tuple[bool, str]:
    exe = shutil.which("notify-send")
    if not exe:
        return False, "notify-send is not installed (package libnotify-bin)"
    try:
        r = subprocess.run([exe, "-a", "AIRA", "-u", urgency, title, body], capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"no notification daemon answered within {timeout:.0f}s"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip() or f"notify-send exit {r.returncode}"
    return True, "sent"
