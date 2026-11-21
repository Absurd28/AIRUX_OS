"""Split an H.264 Annex B byte stream into access units (one video frame each) for the WebSocket.

A new access unit starts at an access unit delimiter, SPS, PPS or SEI, or at a slice whose
first_mb_in_slice is 0 (the first bit of the slice header is 1 for ue(v) = 0), once the current unit
already holds a slice. Slices of one frame (x264's sliced threads) stay together.
"""
from __future__ import annotations

START = b"\x00\x00\x01"


def nal_type(nal: bytes) -> int:
    return nal[0] & 0x1F if nal else -1


def first_slice(nal: bytes) -> bool:
    return len(nal) > 1 and bool(nal[1] & 0x80)


class Splitter:
    def __init__(self):
        self.buf = b""
        self.au: list[bytes] = []
        self.has_slice = False

    def _nal(self, nal: bytes) -> bytes | None:
        """Add one NAL unit (without start code); returns a finished access unit or None."""
        t = nal_type(nal)
        done = None
        starts_new = t in (9, 7, 8, 6) or (t in (1, 5) and first_slice(nal))
        if starts_new and self.has_slice:
            done = b"".join(b"\x00\x00\x00\x01" + n for n in self.au)
            self.au, self.has_slice = [], False
        if t == 9 and not self.au:
            return done                      # delimiters themselves are not needed downstream
        self.au.append(nal)
        if t in (1, 5):
            self.has_slice = True
        return done

    def feed(self, data: bytes) -> list[bytes]:
        """Bytes in, finished access units out. The last NAL unit waits for the next start code."""
        self.buf += data
        out: list[bytes] = []
        pos = self.buf.find(START)
        if pos < 0:
            return out
        while True:
            nxt = self.buf.find(START, pos + 3)
            if nxt < 0:
                break
            end = nxt - 1 if self.buf[nxt - 1] == 0 else nxt      # 4-byte start code
            nal = self.buf[pos + 3:end]
            if nal:
                au = self._nal(nal)
                if au:
                    out.append(au)
            pos = nxt
        self.buf = self.buf[pos:]
        return out

    def flush(self) -> bytes | None:
        if len(self.buf) > 3:
            self._nal(self.buf[3:].lstrip(b"\x00") if self.buf.startswith(b"\x00") else self.buf[3:])
            self.buf = b""
        if self.au and self.has_slice:
            au = b"".join(b"\x00\x00\x00\x01" + n for n in self.au)
            self.au, self.has_slice = [], False
            return au
        return None


def is_key(au: bytes) -> bool:
    """Does this access unit hold an IDR slice?"""
    i = 0
    while True:
        i = au.find(START, i)
        if i < 0 or i + 3 >= len(au):
            return False
        if au[i + 3] & 0x1F == 5:
            return True
        i += 3
