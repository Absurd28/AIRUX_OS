"""A small RFC 6455 WebSocket (server side) for the standard library HTTP server."""
from __future__ import annotations

import base64
import hashlib
import struct
import threading

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TEXT, BINARY, CLOSE, PING, PONG, CONT = 0x1, 0x2, 0x8, 0x9, 0xA, 0x0
MAX_MESSAGE = 4 * 1024 * 1024


class Closed(Exception):
    pass


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def encode(opcode: int, payload: bytes, mask: bytes | None = None) -> bytes:
    n = len(payload)
    head = bytes([0x80 | opcode])
    mbit = 0x80 if mask else 0
    if n < 126:
        head += bytes([mbit | n])
    elif n < 65536:
        head += bytes([mbit | 126]) + struct.pack("!H", n)
    else:
        head += bytes([mbit | 127]) + struct.pack("!Q", n)
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        head += mask
    return head + payload


class WebSocket:
    def __init__(self, rfile, wfile):
        self.rfile, self.wfile = rfile, wfile
        self.lock = threading.Lock()
        self.closed = False

    def _send(self, opcode: int, payload: bytes) -> None:
        if self.closed:
            raise Closed()
        with self.lock:
            try:
                self.wfile.write(encode(opcode, payload))
                self.wfile.flush()
            except (OSError, ValueError) as exc:
                self.closed = True
                raise Closed() from exc

    def send_text(self, text: str) -> None:
        self._send(TEXT, text.encode())

    def send_binary(self, data: bytes) -> None:
        self._send(BINARY, data)

    def close(self, code: int = 1000) -> None:
        if not self.closed:
            try:
                self._send(CLOSE, struct.pack("!H", code))
            except Closed:
                pass
            self.closed = True

    def _read(self, n: int) -> bytes:
        data = self.rfile.read(n)
        if data is None or len(data) < n:
            self.closed = True
            raise Closed()
        return data

    def recv(self) -> tuple[int, bytes]:
        """The next data message (TEXT or BINARY, fragments joined). Answers pings; raises Closed."""
        parts: list[bytes] = []
        first_op = None
        while True:
            b1, b2 = self._read(2)
            fin, op = b1 & 0x80, b1 & 0x0F
            masked, n = b2 & 0x80, b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            if not masked:                       # clients must mask (RFC 6455 5.1)
                self.close(1002)
                raise Closed()
            if n > MAX_MESSAGE or sum(map(len, parts)) + n > MAX_MESSAGE:
                self.close(1009)
                raise Closed()
            mask = self._read(4)
            payload = self._read(n)
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload)) if n < 4096 else _unmask(payload, mask)
            if op == PING:
                self._send(PONG, payload)
                continue
            if op == PONG:
                continue
            if op == CLOSE:
                self.close()
                raise Closed()
            if op != CONT:
                first_op = op
            parts.append(payload)
            if fin:
                return first_op or TEXT, b"".join(parts)


def _unmask(payload: bytes, mask: bytes) -> bytes:
    n = len(payload)
    m = int.from_bytes((mask * (n // 4 + 1))[:n], "big")
    return (int.from_bytes(payload, "big") ^ m).to_bytes(n, "big")
