"""A small SOCKS5 server (RFC 1928, CONNECT only, no authentication) for "only NEOX" mode.

It runs inside the VPN's network namespace, so every connection it makes, and every name it
resolves, goes through the tunnel. NEOX sends host names (socks5 with remote resolution), so DNS
never leaves through the normal connection. Run: python3 -m neox_vpn.socks HOST PORT
"""
from __future__ import annotations

import asyncio
import socket
import struct
import sys

REPLY_OK, REPLY_FAIL, REPLY_NOT_ALLOWED, REPLY_NET, REPLY_HOST, REPLY_REFUSED, REPLY_CMD, REPLY_ATYP = 0, 1, 2, 3, 4, 5, 7, 8


def reply(code: int) -> bytes:
    return bytes([5, code, 0, 1, 0, 0, 0, 0, 0, 0])


async def _pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, timeout: float = 30) -> None:
    try:
        head = await asyncio.wait_for(reader.readexactly(2), timeout)
        if head[0] != 5:
            return
        methods = await reader.readexactly(head[1])
        if 0 not in methods:
            writer.write(b"\x05\xff")
            return
        writer.write(b"\x05\x00")
        ver, cmd, _, atyp = await asyncio.wait_for(reader.readexactly(4), timeout)
        if atyp == 1:
            host = socket.inet_ntop(socket.AF_INET, await reader.readexactly(4))
        elif atyp == 4:
            host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
        elif atyp == 3:
            n = (await reader.readexactly(1))[0]
            try:                                 # names arrive as ASCII (punycode for IDNs)
                host = (await reader.readexactly(n)).decode("ascii")
            except UnicodeDecodeError:
                writer.write(reply(REPLY_HOST))
                return
        else:
            writer.write(reply(REPLY_ATYP))
            return
        port = struct.unpack("!H", await reader.readexactly(2))[0]
        if cmd != 1:
            writer.write(reply(REPLY_CMD))
            return
        try:
            ur, uw = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        except socket.gaierror:
            writer.write(reply(REPLY_HOST))
            return
        except ConnectionRefusedError:
            writer.write(reply(REPLY_REFUSED))
            return
        except (OSError, asyncio.TimeoutError):
            writer.write(reply(REPLY_NET))            # no route: the tunnel is down (nothing leaks)
            return
        writer.write(reply(REPLY_OK))
        await writer.drain()
        await asyncio.gather(_pipe(reader, uw), _pipe(ur, writer))
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
        pass
    except Exception as exc:                     # never leave a client hanging on an unexpected error
        print(f"neox-vpn socks: {exc!r}", file=sys.stderr, flush=True)
        try:
            writer.write(reply(REPLY_FAIL))
        except Exception:
            pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def serve(host: str, port: int) -> asyncio.base_events.Server:
    return await asyncio.start_server(handle, host, port, reuse_address=True)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    host, port = argv[0], int(argv[1])

    async def run():
        srv = await serve(host, port)
        print(f"neox-vpn socks on {host}:{port}", flush=True)
        async with srv:
            await srv.serve_forever()
    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
