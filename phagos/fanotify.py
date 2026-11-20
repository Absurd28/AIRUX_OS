"""fanotify(7) through ctypes: the kernel tells Phagos about files as they are written and run.

Permission events (FAN_OPEN_EXEC_PERM) make the kernel wait for Phagos's answer before a program
starts; Phagos must always answer, quickly (the daemon answers "allow" when a scan runs out of
time, and the kernel allows everything pending if Phagos exits). Needs root (CAP_SYS_ADMIN).
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import struct
from dataclasses import dataclass

FAN_CLOSE_WRITE = 0x00000008
FAN_OPEN_EXEC_PERM = 0x00040000
FAN_Q_OVERFLOW = 0x00004000
FAN_CLOEXEC = 0x00000001
FAN_CLASS_CONTENT = 0x00000004
FAN_UNLIMITED_QUEUE = 0x00000010
FAN_UNLIMITED_MARKS = 0x00000020
FAN_MARK_ADD = 0x00000001
FAN_MARK_REMOVE = 0x00000002
FAN_MARK_MOUNT = 0x00000010
FAN_ALLOW = 0x01
FAN_DENY = 0x02
FAN_NOFD = -1

METADATA = struct.Struct("=IBBHQii")       # event_len, vers, reserved, metadata_len, mask, fd, pid
RESPONSE = struct.Struct("=iI")            # fd, response
METADATA_VERSION = 3

_libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
_libc.fanotify_init.argtypes = [ctypes.c_uint, ctypes.c_uint]
_libc.fanotify_init.restype = ctypes.c_int
_libc.fanotify_mark.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_uint64, ctypes.c_int, ctypes.c_char_p]
_libc.fanotify_mark.restype = ctypes.c_int


class FanotifyError(OSError):
    pass


@dataclass
class Event:
    mask: int
    fd: int
    pid: int

    @property
    def is_perm(self) -> bool:
        return bool(self.mask & FAN_OPEN_EXEC_PERM)

    def path(self) -> str:
        try:
            return os.readlink(f"/proc/self/fd/{self.fd}")
        except OSError:
            return ""


def parse(buf: bytes) -> list[Event]:
    """Events from one read() of a fanotify descriptor."""
    out, off = [], 0
    while off + METADATA.size <= len(buf):
        event_len, vers, _r, _ml, mask, fd, pid = METADATA.unpack_from(buf, off)
        if vers != METADATA_VERSION or event_len < METADATA.size:
            raise FanotifyError(0, f"unexpected fanotify metadata (version {vers}, length {event_len})")
        out.append(Event(mask, fd, pid))
        off += event_len
    return out


def response(fd: int, allow: bool) -> bytes:
    return RESPONSE.pack(fd, FAN_ALLOW if allow else FAN_DENY)


class Fanotify:
    def __init__(self, flags: int = FAN_CLOEXEC | FAN_CLASS_CONTENT | FAN_UNLIMITED_QUEUE | FAN_UNLIMITED_MARKS,
                 event_flags: int = os.O_RDONLY | os.O_LARGEFILE | os.O_CLOEXEC):
        fd = _libc.fanotify_init(flags, event_flags)
        if fd < 0:
            e = ctypes.get_errno()
            raise FanotifyError(e, f"fanotify_init: {os.strerror(e)}")
        self.fd = fd

    def mark_mount(self, path: str, mask: int) -> None:
        r = _libc.fanotify_mark(self.fd, FAN_MARK_ADD | FAN_MARK_MOUNT, mask, -100, path.encode())   # AT_FDCWD
        if r < 0:
            e = ctypes.get_errno()
            raise FanotifyError(e, f"fanotify_mark {path}: {os.strerror(e)}")

    def read(self, size: int = 64 * 1024) -> list[Event]:
        return parse(os.read(self.fd, size))

    def respond(self, ev: Event, allow: bool) -> None:
        os.write(self.fd, response(ev.fd, allow))

    def close(self) -> None:
        os.close(self.fd)
