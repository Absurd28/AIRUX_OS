"""Desktop agent tools: files, apps, clipboard.

Deletes go to the freedesktop trash (~/.local/share/Trash), never rm, so they can be undone.
Windows, screenshots and AT-SPI arrive with the shell in phase 7.
"""
from __future__ import annotations

import datetime as dt
import fnmatch
import os
import shutil
import subprocess
import urllib.parse
from pathlib import Path

from .base import I, Registry, Risk, S, Tool, ToolError, expand, run, truncate

TEXT_LIMIT = 20000
APP_DIRS = [Path("/usr/share/applications"), Path("/usr/local/share/applications"),
            Path.home() / ".local/share/applications"]


def _must_exist(path: str) -> Path:
    p = expand(path)
    if not p.exists():
        raise ToolError(f"no such file or folder: {p}")
    return p


def list_dir(path: str = "~", show_hidden: bool = False) -> dict:
    p = _must_exist(path)
    if not p.is_dir():
        raise ToolError(f"not a folder: {p}")
    items = []
    for c in sorted(p.iterdir(), key=lambda x: x.name.lower()):
        if not show_hidden and c.name.startswith("."):
            continue
        st = c.lstat()
        items.append({"name": c.name, "type": "dir" if c.is_dir() else "file", "bytes": st.st_size,
                      "modified": dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="minutes")})
    return {"path": str(p), "count": len(items), "items": items[:200]}


def find_files(pattern: str, root: str = "~", max_results: int = 50) -> dict:
    base = _must_exist(root)
    pat = pattern if any(ch in pattern for ch in "*?[") else f"*{pattern}*"
    hits = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("node_modules", "__pycache__")]
        for name in filenames + dirnames:
            if fnmatch.fnmatch(name.lower(), pat.lower()):
                hits.append(str(Path(dirpath) / name))
                if len(hits) >= max_results:
                    return {"root": str(base), "pattern": pat, "matches": hits, "truncated": True}
    return {"root": str(base), "pattern": pat, "matches": hits, "truncated": False}


def read_file(path: str, max_chars: int = 8000) -> dict:
    p = _must_exist(path)
    if p.is_dir():
        raise ToolError(f"is a folder, not a file: {p}")
    raw = p.read_bytes()[: max(1, min(max_chars, TEXT_LIMIT)) * 4]
    if b"\0" in raw[:4096]:
        raise ToolError(f"binary file, not shown: {p}")
    text = raw.decode("utf-8", errors="replace")
    return {"path": str(p), "bytes": p.stat().st_size, "text": truncate(text, min(max_chars, TEXT_LIMIT))}


def write_file(path: str, content: str, append: bool = False) -> dict:
    p = expand(path)
    if not p.parent.exists():
        raise ToolError(f"folder does not exist: {p.parent}")
    with p.open("a" if append else "w") as f:
        f.write(content)
    return {"path": str(p), "bytes": p.stat().st_size, "appended": append}


def _write_risk(args: dict) -> set[Risk]:
    p = expand(args["path"])
    if p.exists() and not args.get("append"):
        return {Risk.OVERWRITE}
    return {Risk.WRITE}


def move_path(source: str, destination: str) -> dict:
    src = _must_exist(source)
    dst = expand(destination)
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists():
        raise ToolError(f"destination already exists: {dst}")
    if not dst.parent.exists():
        raise ToolError(f"destination folder does not exist: {dst.parent}")
    shutil.move(str(src), str(dst))
    return {"moved_from": str(src), "moved_to": str(dst)}


def _move_risk(args: dict) -> set[Risk]:
    dst = expand(args["destination"])
    src_name = expand(args["source"]).name
    target = dst / src_name if dst.is_dir() else dst
    return {Risk.OVERWRITE} if target.exists() else {Risk.WRITE}


def trash_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "Trash"


def delete_path(path: str) -> dict:
    """Move to the freedesktop trash with a .trashinfo record (restorable)."""
    p = _must_exist(path)
    if p == Path.home() or p == Path("/") or len(p.parts) <= 2:
        raise ToolError(f"refusing to delete {p}")
    t = trash_dir()
    (t / "files").mkdir(parents=True, exist_ok=True)
    (t / "info").mkdir(parents=True, exist_ok=True)
    name, n = p.name, 1
    while (t / "files" / name).exists():
        n += 1
        name = f"{p.stem}.{n}{p.suffix}"
    (t / "info" / f"{name}.trashinfo").write_text(
        "[Trash Info]\nPath=" + urllib.parse.quote(str(p)) +
        "\nDeletionDate=" + dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S") + "\n")
    shutil.move(str(p), str(t / "files" / name))
    return {"deleted": str(p), "trash_location": str(t / "files" / name), "restorable": True}


def _find_desktop_entry(name: str) -> Path | None:
    want = name.lower().removesuffix(".desktop")
    for d in APP_DIRS:
        if not d.exists():
            continue
        for f in d.glob("*.desktop"):
            if f.stem.lower() == want:
                return f
        for f in d.glob("*.desktop"):
            for line in f.read_text(errors="replace").splitlines():
                if line.startswith("Name=") and line[5:].strip().lower() == want:
                    return f
    return None


def open_app(name: str, target: str = "") -> dict:
    entry = _find_desktop_entry(name)
    exe = None if entry else shutil.which(name)
    if entry is None and exe is None:
        raise ToolError(f"no installed application called {name!r}")
    if target:
        tp = expand(target) if not target.startswith(("http://", "https://")) else None
        if tp is not None and not tp.exists():
            raise ToolError(f"no such file to open: {tp}")
    if entry is not None:
        cmd = ["gtk-launch", entry.stem] + ([target] if target else [])
    else:
        cmd = [exe] + ([target] if target else [])
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return {"launched": entry.stem if entry else exe, "pid": proc.pid, "target": target or None}


def clipboard(action: str, text: str = "") -> dict:
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        get_cmd, set_cmd = ["wl-paste", "--no-newline"], ["wl-copy"]
    elif shutil.which("xclip"):
        get_cmd, set_cmd = ["xclip", "-o", "-selection", "clipboard"], ["xclip", "-i", "-selection", "clipboard"]
    else:
        raise ToolError("no clipboard tool available (install wl-clipboard or xclip)")
    if action == "get":
        return {"text": truncate(run(get_cmd, timeout=5), 4000)}
    run(set_cmd, timeout=5, input_text=text)
    return {"copied_chars": len(text)}


def register(reg: Registry) -> None:
    A = "desktop"
    reg.add(Tool("list_dir", A, "list the files and folders in a folder",
                 {"path": S("folder path"), "show_hidden": {"type": "boolean"}}, ["path"], list_dir))
    reg.add(Tool("find_files", A, "find files or folders by name pattern under a folder",
                 {"pattern": S("name or glob, e.g. *.pdf or report"), "root": S("folder to search, default ~"),
                  "max_results": I("limit", 1, 500)}, ["pattern"], find_files))
    reg.add(Tool("read_file", A, "read a text file", {"path": S("file path"), "max_chars": I("limit", 1, 20000)},
                 ["path"], read_file))
    reg.add(Tool("write_file", A, "write text to a file (creates it, overwrites it, or appends)",
                 {"path": S("file path"), "content": S("text to write"), "append": {"type": "boolean"}},
                 ["path", "content"], write_file, _write_risk))
    reg.add(Tool("move_path", A, "move or rename a file or folder",
                 {"source": S("existing path"), "destination": S("new path or folder")},
                 ["source", "destination"], move_path, _move_risk))
    reg.add(Tool("delete_path", A, "delete a file or folder (moves it to the trash)",
                 {"path": S("path to delete")}, ["path"], delete_path, frozenset({Risk.DELETE})))
    reg.add(Tool("open_app", A, "open an installed application, optionally with a file or URL",
                 {"name": S("application name, e.g. firefox, code, libreoffice"),
                  "target": S("file path or URL to open with it")}, ["name"], open_app,
                 frozenset({Risk.WRITE})))
    reg.add(Tool("clipboard", A, "get the clipboard text or copy text to it",
                 {"action": S("get or set", enum=["get", "set"]), "text": S("text to copy for set")},
                 ["action"], clipboard,
                 lambda a: {Risk.WRITE} if a.get("action") == "set" else {Risk.READ}))
