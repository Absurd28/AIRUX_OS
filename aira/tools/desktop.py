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

from .base import (I, Registry, Risk, S, Tool, ToolError, exists_dir, exists_path, expand, parent_dir, run,
                   truncate)

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


def _launch_command(entry: Path, target: str = "") -> list[str]:
    """How to start a .desktop entry on this desktop: gtk-launch (GTK desktops), KDE's kioclient
    (Plasma, which has no gtk-launch), or gio."""
    extra = [target] if target else []
    if shutil.which("gtk-launch"):
        return ["gtk-launch", entry.stem] + extra
    for kio in ("kioclient6", "kioclient"):
        if shutil.which(kio):
            return [kio, "exec", str(entry)] + extra
    if shutil.which("gio"):
        return ["gio", "launch", str(entry)] + extra
    raise ToolError("no way to start applications here (gtk-launch, kioclient or gio is needed)")


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
        cmd = _launch_command(entry, target)
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


def _write_entities(a: dict) -> list:
    if a.get("append"):
        return [exists_path(a["path"], "file")]
    return [parent_dir(a["path"])]


def _write_readback(a: dict, result) -> tuple[bool, str]:
    p = expand(a["path"])
    if not p.is_file():
        return False, f"{p} does not exist after writing"
    text = p.read_text(errors="replace")
    ok = text.endswith(a["content"]) if a.get("append") else text == a["content"]
    return ok, f"{p} contains the {'appended ' if a.get('append') else ''}text" if ok else f"{p} content differs"


def _move_entities(a: dict) -> list:
    dst = None
    try:
        d = expand(a["destination"])
        dst = ("folder", str(d if d.is_dir() else d.parent), d.is_dir() or d.parent.is_dir())
    except ToolError:
        dst = ("folder", a["destination"], False)
    return [exists_path(a["source"]), dst]


def _move_readback(a: dict, result) -> tuple[bool, str]:
    src, dst = Path(result["moved_from"]), Path(result["moved_to"])
    ok = dst.exists() and not src.exists()
    return ok, f"{dst} exists and {src} is gone" if ok else f"move not confirmed: dst exists={dst.exists()}, src exists={src.exists()}"


def _delete_readback(a: dict, result) -> tuple[bool, str]:
    p, t = Path(result["deleted"]), Path(result["trash_location"])
    ok = not p.exists() and t.exists()
    return ok, f"{p} is gone and is in the trash" if ok else f"delete not confirmed: still exists={p.exists()}"


def _app_entities(a: dict) -> list:
    name = a["name"]
    found = _find_desktop_entry(name) is not None or shutil.which(name) is not None
    out = [("application", name, found)]
    t = a.get("target", "")
    if t and not t.startswith(("http://", "https://")):
        out.append(exists_path(t, "file"))
    return out


def _app_readback(a: dict, result) -> tuple[bool, str]:
    import time
    time.sleep(1.0)
    alive = Path(f"/proc/{result['pid']}").exists()
    return alive, f"{result['launched']} is running (pid {result['pid']})" if alive else \
        f"{result['launched']} exited right after starting"


def _clipboard_readback(a: dict, result) -> tuple[bool, str]:
    got = clipboard("get")["text"]
    ok = got == a.get("text", "")
    return ok, "clipboard holds the text" if ok else "clipboard content differs"


def make_browser_tool(session):
    from ..browser import BrowserError, query_in_url

    def browser(action: str, query: str = "", url: str = "", text: str = "") -> dict:
        try:
            if action == "open":
                return session.open(url)
            if action == "search":
                if not query.strip():
                    raise ToolError("say what to search for")
                return {"query": query, **session.search(query)}
            if action == "read":
                return session.read()
            if action == "click":
                if not text.strip():
                    raise ToolError("say which link or button to click")
                return session.click(text)
            if action == "back":
                return session.back()
        except BrowserError as exc:
            raise ToolError(f"browser: {exc}") from None
        raise ToolError(f"unknown browser action {action}")

    def risk(a: dict) -> set[Risk]:
        # anything that loads a page contacts a website: data leaves the machine
        if a.get("action") == "read" or (a.get("action") == "open" and not a.get("url")):
            return {Risk.READ}
        return {Risk.SEND_OFF}

    def readback(a: dict, result) -> tuple[bool, str]:
        now = session.read()
        if a["action"] == "search":
            # a results page: the query in its URL, its words in the visible text, and links out to results
            # (a search engine's bot-check page fails the last two)
            words = [w for w in a["query"].lower().split() if len(w) > 2]
            in_text = all(w in now["text"].lower() for w in words)
            ok = query_in_url(now["url"], a["query"]) and in_text and now.get("outbound_links", 0) >= 3
            return ok, (f"the browser shows results for '{a['query']}' ({now['title']}, "
                        f"{now.get('outbound_links', 0)} result links)" if ok
                        else f"the browser is on {now['url']} ({now['title']}), which does not look like results "
                             f"for '{a['query']}'")
        if a["action"] == "open":
            want = a.get("url", "")
            ok = (not want) or want.split("//")[-1].split("/")[0] in now["url"]
            return ok, f"the browser is on {now['url']}"
        return True, f"the browser is on {now['url']} ({now['title']})"

    return browser, risk, readback


def register(reg: Registry, browser_session=None) -> None:
    A = "desktop"
    reg.add(Tool("list_dir", A, "list the files and folders in a folder",
                 {"path": S("folder path"), "show_hidden": {"type": "boolean"}}, ["path"], list_dir,
                 entities=lambda a: [exists_dir(a["path"])]))
    reg.add(Tool("find_files", A, "find files or folders by name pattern under a folder",
                 {"pattern": S("name or glob, e.g. *.pdf or report"), "root": S("folder to search, default ~"),
                  "max_results": I("limit", 1, 500)}, ["pattern"], find_files,
                 entities=lambda a: [exists_dir(a["root"])] if a.get("root") else []))
    reg.add(Tool("read_file", A, "read a text file", {"path": S("file path"), "max_chars": I("limit", 1, 20000)},
                 ["path"], read_file, entities=lambda a: [exists_path(a["path"], "file")]))
    reg.add(Tool("write_file", A, "write text to a file (creates it, overwrites it, or appends)",
                 {"path": S("file path"), "content": S("text to write"), "append": {"type": "boolean"}},
                 ["path", "content"], write_file, _write_risk, entities=_write_entities,
                 readback=_write_readback, changes_state=True))
    reg.add(Tool("move_path", A, "move or rename a file or folder",
                 {"source": S("existing path"), "destination": S("new path or folder")},
                 ["source", "destination"], move_path, _move_risk, entities=_move_entities,
                 readback=_move_readback, changes_state=True))
    reg.add(Tool("delete_path", A, "delete a file or folder (moves it to the trash)",
                 {"path": S("path to delete")}, ["path"], delete_path, frozenset({Risk.DELETE}),
                 entities=lambda a: [exists_path(a["path"])], readback=_delete_readback, changes_state=True))
    reg.add(Tool("open_app", A, "open an installed application, optionally with a file or URL",
                 {"name": S("application name, e.g. firefox, code, libreoffice"),
                  "target": S("file path or URL to open with it")}, ["name"], open_app,
                 frozenset({Risk.WRITE}), entities=_app_entities, readback=_app_readback, changes_state=True))
    reg.add(Tool("clipboard", A, "get the clipboard text or copy text to it",
                 {"action": S("get or set", enum=["get", "set"]), "text": S("text to copy for set")},
                 ["action"], clipboard,
                 lambda a: {Risk.WRITE} if a.get("action") == "set" else {Risk.READ},
                 readback=lambda a, r: _clipboard_readback(a, r) if a.get("action") == "set" else (True, "read"),
                 changes_state=lambda a: a.get("action") == "set"))
    if browser_session is not None:
        fn, risk, rb = make_browser_tool(browser_session)
        reg.add(Tool("browser", A, "control the web browser: open a page, search the web, read the page, "
                     "click a link or button by its text, go back. To search the web, use action search with the "
                     "user's search terms (do not open a search engine's home page)",
                     {"action": S("what to do", enum=["open", "search", "read", "click", "back"]),
                      "query": S("search terms, for search"), "url": S("address, for open"),
                      "text": S("link or button text, for click")}, ["action"], fn, risk,
                     readback=rb, changes_state=lambda a: a.get("action") != "read"))
