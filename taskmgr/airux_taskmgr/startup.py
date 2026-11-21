"""Startup apps: XDG autostart entries (/etc/xdg/autostart and ~/.config/autostart).

A user file with the same name overrides the system one. Disabling a system entry writes a user copy
with Hidden=true (the XDG way); enabling removes that override again. Entries for other desktops
(OnlyShowIn without KDE, NotShowIn with KDE) are not listed.
"""
from __future__ import annotations

import os
from pathlib import Path

DESKTOP = "KDE"


def system_dirs() -> list[Path]:
    dirs = os.environ.get("XDG_CONFIG_DIRS", "/etc/xdg").split(":")
    return [Path(d) / "autostart" for d in dirs if d]


def user_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart"


def parse(path: Path) -> dict:
    out, section = {}, ""
    try:
        for line in path.read_text(errors="replace").splitlines():
            s = line.strip()
            if s.startswith("["):
                section = s
            elif section == "[Desktop Entry]" and "=" in s and not s.startswith("#"):
                k, _, v = s.partition("=")
                out.setdefault(k.strip(), v.strip())
    except OSError:
        pass
    return out


def _shown(e: dict) -> bool:
    only = [x for x in e.get("OnlyShowIn", "").split(";") if x]
    never = [x for x in e.get("NotShowIn", "").split(";") if x]
    return (not only or DESKTOP in only) and DESKTOP not in never


def _enabled(e: dict) -> bool:
    return e.get("Hidden", "false").lower() != "true" and e.get("X-GNOME-Autostart-enabled", "true").lower() != "false"


def entries(sys_dirs: list[Path] | None = None, udir: Path | None = None) -> list[dict]:
    sys_dirs = system_dirs() if sys_dirs is None else sys_dirs
    udir = udir or user_dir()
    files: dict[str, dict] = {}
    for d in reversed(sys_dirs):                 # earlier dirs win
        for f in sorted(d.glob("*.desktop")):
            files[f.name] = {"system": f}
    for f in sorted(udir.glob("*.desktop")) if udir.is_dir() else []:
        files.setdefault(f.name, {})["user"] = f
    out = []
    for name, src in files.items():
        e = parse(src.get("user") or src["system"])
        base = parse(src["system"]) if "system" in src else e
        if not _shown(base):
            continue
        out.append({"id": name, "name": base.get("Name") or e.get("Name") or name[:-8], "icon": base.get("Icon", ""),
                    "command": e.get("Exec", ""), "enabled": _enabled(e),
                    "scope": "user" if "system" not in src else "system",
                    "overridden": "user" in src and "system" in src})
    return sorted(out, key=lambda x: x["name"].lower())


def find(entry_id: str, sys_dirs: list[Path] | None = None, udir: Path | None = None) -> dict | None:
    return next((e for e in entries(sys_dirs, udir) if e["id"] == entry_id), None)


def target(entry_id: str, udir: Path | None = None) -> Path:
    """The file a change writes: always the user's copy (never /etc)."""
    if "/" in entry_id or not entry_id.endswith(".desktop") or entry_id.startswith("."):
        raise ValueError(f"not an autostart entry: {entry_id!r}")
    return (udir or user_dir()) / entry_id


def set_enabled(entry_id: str, enabled: bool, sys_dirs: list[Path] | None = None, udir: Path | None = None) -> dict:
    sys_dirs = system_dirs() if sys_dirs is None else sys_dirs
    e = find(entry_id, sys_dirs, udir)
    if e is None:
        raise ValueError(f"no startup app {entry_id!r}")
    t = target(entry_id, udir)
    sys_file = next((d / entry_id for d in sys_dirs if (d / entry_id).is_file()), None)
    if enabled and sys_file is not None and t.is_file() and parse(t).get(OVERRIDE) == "true":
        t.unlink()                                # the override this module wrote: remove it
    else:
        override = not t.is_file()                # a new user copy of a system entry
        src = t if t.is_file() else sys_file
        lines = src.read_text(errors="replace").splitlines() if src else []
        lines = [ln for ln in lines if not ln.strip().startswith(("Hidden=", "X-GNOME-Autostart-enabled="))]
        if not lines:
            lines = ["[Desktop Entry]"]
        i = next((n for n, ln in enumerate(lines) if ln.strip() == "[Desktop Entry]"), 0)
        lines.insert(i + 1, f"Hidden={'false' if enabled else 'true'}")
        if override:
            lines.insert(i + 2, f"{OVERRIDE}=true")
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text("\n".join(lines) + "\n")
    return find(entry_id, sys_dirs, udir) or {}


OVERRIDE = "X-AIRUX-TaskManager-Override"
