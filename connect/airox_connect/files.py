"""Files between the PC and the phone.

Browsing is limited to the user's home folder (symlinks that lead out of it are refused; hidden files
only on request). PC -> phone: an *offer* (made through the broker, which asks first) gives transfer
ids the phone downloads with HTTP Range, so a broken transfer resumes. Phone -> PC: resumable chunked
upload into ~/Downloads/AIROX Connect/, checked against the SHA-256 the phone sends; an existing file
is never overwritten (a new name is chosen).
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

OFFER_SECONDS = 6 * 3600
NAME_RE = re.compile(r"^[^/\x00]{1,255}$")


class FilesError(Exception):
    pass


def home() -> Path:
    return Path(os.path.realpath(Path.home()))


def resolve(path: str, show_hidden: bool = False, root: Path | None = None) -> Path:
    root = root or home()
    raw = (path or "~").strip()
    if raw == "~" or raw.startswith("~/"):
        raw = str(root) + raw[1:]
    p = Path(raw)
    if not p.is_absolute():
        p = root / p
    real = Path(os.path.realpath(p))
    if real != root and root not in real.parents:
        raise FilesError("only files in your home folder can be reached from the phone")
    if not show_hidden and any(part.startswith(".") for part in real.relative_to(root).parts):
        raise FilesError("that is a hidden file or folder")
    return real


def tilde(p: Path, root: Path | None = None) -> str:
    root = root or home()
    return "~" if p == root else "~/" + str(p.relative_to(root))


def kind(p: Path) -> str:
    if p.is_dir():
        return "folder"
    mime = mimetypes.guess_type(p.name)[0] or ""
    for k in ("image", "video", "audio"):
        if mime.startswith(k + "/"):
            return k
    if mime == "application/pdf" or p.suffix.lower() in (".doc", ".docx", ".odt", ".txt", ".md", ".tex"):
        return "document"
    if p.suffix.lower() in (".zip", ".tar", ".gz", ".xz", ".7z", ".zst", ".deb", ".apk"):
        return "archive"
    return "file"


def list_dir(path: str = "~", show_hidden: bool = False, root: Path | None = None) -> dict:
    d = resolve(path, show_hidden, root)
    if not d.is_dir():
        raise FilesError(f"not a folder: {path}")
    items = []
    try:
        entries = list(os.scandir(d))
    except PermissionError as exc:
        raise FilesError("that folder cannot be read") from exc
    for e in entries:
        if not show_hidden and e.name.startswith("."):
            continue
        try:
            st = e.stat()
            is_dir = e.is_dir()
        except OSError:
            continue
        p = Path(e.path)
        items.append({"name": e.name, "path": tilde(p, root or home()), "dir": is_dir,
                      "size": None if is_dir else st.st_size, "mtime": st.st_mtime,
                      "kind": "folder" if is_dir else kind(p), "mime": None if is_dir else mimetypes.guess_type(e.name)[0]})
    items.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    parent = None if d == (root or home()) else tilde(d.parent, root or home())
    return {"path": tilde(d, root or home()), "parent": parent, "items": items}


def sha256_file(p: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def unique_name(folder: Path, name: str) -> Path:
    name = Path(name).name.strip() or "file"
    target = folder / name
    stem, suffix = Path(name).stem, "".join(Path(name).suffixes[-1:])
    n = 1
    while target.exists():
        target = folder / f"{stem} ({n}){suffix}"
        n += 1
    return target


class Transfers:
    """Offers (PC -> phone) and uploads (phone -> PC), per device."""

    def __init__(self, inbox: Path, root: Path | None = None):
        self.inbox = inbox
        self.root = root
        self.offers: dict[str, dict] = {}
        self.uploads: dict[str, dict] = {}
        self.lock = threading.Lock()

    # ---- PC -> phone
    def offer(self, device: str, paths: list[str], origin: str = "phone") -> list[dict]:
        out = []
        for raw in paths:
            p = resolve(raw, show_hidden=True, root=self.root)
            if not p.is_file():
                raise FilesError(f"not a file: {raw}")
            tid = secrets.token_urlsafe(12)
            item = {"tid": tid, "device": device, "path": str(p), "name": p.name, "size": p.stat().st_size,
                    "mime": mimetypes.guess_type(p.name)[0] or "application/octet-stream", "origin": origin,
                    "expires": time.time() + OFFER_SECONDS, "sha256": None, "sent": 0}
            with self.lock:
                self.offers[tid] = item
            out.append({k: item[k] for k in ("tid", "name", "size", "mime", "origin")})
        return out

    def get_offer(self, device: str, tid: str) -> dict:
        with self.lock:
            o = self.offers.get(tid)
        if o is None or o["device"] != device or o["expires"] < time.time():
            raise FilesError("that transfer is not available (expired or not for this device)")
        if not Path(o["path"]).is_file():
            raise FilesError("the file is not there any more")
        return o

    def offer_hash(self, device: str, tid: str) -> str:
        o = self.get_offer(device, tid)
        if o["sha256"] is None:
            o["sha256"] = sha256_file(Path(o["path"]))
        return o["sha256"]

    def pending(self, device: str, origin: str | None = None) -> list[dict]:
        now = time.time()
        with self.lock:
            return [{k: o[k] for k in ("tid", "name", "size", "mime", "origin")} for o in self.offers.values()
                    if o["device"] == device and o["expires"] > now and (origin is None or o["origin"] == origin)]

    def pending_all(self) -> list[dict]:
        now = time.time()
        with self.lock:
            return [{k: o[k] for k in ("tid", "device", "name", "size", "origin", "sent")} for o in self.offers.values()
                    if o["expires"] > now]

    def done(self, device: str, tid: str) -> None:
        with self.lock:
            o = self.offers.get(tid)
            if o and o["device"] == device:
                del self.offers[tid]

    # ---- phone -> PC
    def _partial(self, device: str, uid: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", uid):
            raise FilesError("bad upload id")
        d = self.inbox / ".partial"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{device}-{uid}.part"

    def upload_status(self, device: str, uid: str) -> int:
        p = self._partial(device, uid)
        return p.stat().st_size if p.exists() else 0

    def upload_chunk(self, device: str, uid: str, name: str, total: int, sha256: str, start: int, data_iter) -> dict:
        """Append bytes at `start` (must equal what is already there). When the file is complete its
        SHA-256 is checked and it is moved into the inbox under a name that does not exist yet."""
        if not NAME_RE.match(name or "") or name in (".", ".."):
            raise FilesError("bad file name")
        if not re.fullmatch(r"[0-9a-f]{64}", sha256 or ""):
            raise FilesError("the upload needs the file's SHA-256")
        if total < 0:
            raise FilesError("bad size")
        p = self._partial(device, uid)
        have = p.stat().st_size if p.exists() else 0
        if start != have:
            raise FilesError(f"resume at byte {have}, not {start}")
        free = shutil.disk_usage(self.inbox.parent if not self.inbox.exists() else self.inbox).free
        if total - have > free - 256 * 1024 * 1024:
            raise FilesError("not enough free space on the PC")
        with p.open("ab") as f:
            for chunk in data_iter:
                if have + len(chunk) > total:
                    raise FilesError("more bytes than the size given")
                f.write(chunk)
                have += len(chunk)
        if have < total:
            return {"received": have, "complete": False}
        got = sha256_file(p)
        if got != sha256:
            p.unlink()
            raise FilesError("the file arrived damaged (SHA-256 differs); send it again")
        self.inbox.mkdir(parents=True, exist_ok=True)
        with self.lock:
            target = unique_name(self.inbox, name)
            os.link(p, target)                # never replaces an existing file (link fails if it exists)
            p.unlink()
        return {"received": have, "complete": True, "saved": str(target), "name": target.name, "sha256": got}


def read_range(path: Path, rng: str | None, size: int) -> tuple[int, int]:
    """Parse a single `bytes=a-b` range; returns (start, end inclusive)."""
    if not rng:
        return 0, size - 1
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
    if not m or (not m.group(1) and not m.group(2)):
        raise FilesError("bad range")
    if m.group(1):
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else size - 1
    else:
        start, end = max(size - int(m.group(2)), 0), size - 1
    if start >= size or end < start:
        raise FilesError("range not satisfiable")
    return start, min(end, size - 1)
