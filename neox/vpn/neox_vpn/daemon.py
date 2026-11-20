"""neox-vpnd: NEOX's free VPN service (root). One connection at a time.

  backends  riseup (free, anonymous OpenVPN) | tor | wireguard (the user's own configs)
  scopes    neox   the tunnel lives in the "neoxvpn" namespace; NEOX reaches it through SOCKS
            system the whole computer: NetworkManager (Riseup, WireGuard) or nftables (Tor)
  hopping   every x minutes (the user's number) to the next place on the user's list

API: JSON lines on /run/neox-vpn/vpn.sock. Anyone may ask for status, providers and check_ip;
connect, disconnect and WireGuard imports need polkit's org.airux.neox.vpn (allowed for the person
at the active desktop, like NetworkManager's own VPN switch).
"""
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import netns, system
from .hop import Hopper
from .riseup import Riseup, RiseupError
from .tor import COUNTRIES, SOCKS_PORT as TOR_SOCKS, TorBackend, TorError
from .wireguard import Store, WireGuardError, setconf

log = logging.getLogger("neox-vpnd")

STATE = Path(os.environ.get("NEOX_VPN_STATE", "/var/lib/neox-vpn"))
RUN = Path(os.environ.get("NEOX_VPN_RUN", "/run/neox-vpn"))
SOCKET = RUN / "vpn.sock"
UP_SCRIPT = os.environ.get("NEOX_VPN_UP", str(netns.LIB / "neox-vpn-up"))
POLKIT_ACTION = "org.airux.neox.vpn"
BACKENDS = ("riseup", "tor", "wireguard")
SCOPES = ("neox", "system")
WG_DEV = "nxwg0"
TUNNEL_TIMEOUT = 40
GATEWAY_TRIES = 3
PROBES = ("9.9.9.9", "1.1.1.1")


class ApiError(Exception):
    pass


ERRORS = (ApiError, RiseupError, TorError, WireGuardError, netns.NetnsError, system.SystemError_, ValueError,
          subprocess.TimeoutExpired)


def spawn(cmd: list[str], timeout: float, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, start_new_session=True, **kw)


class VPN:
    def __init__(self, state: Path = STATE, run: Path = RUN):
        self.state, self.run = state, run
        self.riseup = Riseup(state / "riseup")
        self.tor = TorBackend(state, run)
        self.wg = Store(state / "wireguard")
        self.op = threading.Lock()                 # connect / disconnect / hop, one at a time
        self.conn: dict | None = None              # what is up now (shown by status)
        self.hopper: Hopper | None = None
        self.openvpn: subprocess.Popen | None = None
        self.socks: subprocess.Popen | None = None
        self.last_error: str | None = None
        self.stopping = threading.Event()

    # ------------------------------------------------------------ status
    def status(self) -> dict:
        c = dict(self.conn) if self.conn else None
        if c and self.hopper:
            c["hop"] = {"minutes": self.hopper.minutes, "places": self.hopper.countries, "order": self.hopper.order,
                        "seconds_left": self.hopper.seconds_left()}
        return {"connected": c is not None and c.get("state") == "up", "connection": c, "error": self.last_error}

    def providers(self) -> dict:
        out = {"tor": [{"id": cc, "country": cc.upper()} for cc in COUNTRIES],
               "wireguard": self.wg.items(), "riseup": [], "riseup_error": None}
        try:
            out["riseup"] = self.riseup.locations()
        except RiseupError as exc:
            out["riseup_error"] = str(exc)
        return out

    def proxy(self) -> dict | None:
        """What NEOX should use as its proxy (None: direct, the whole computer is on the VPN)."""
        c = self.conn
        if not c or c["scope"] != "neox":
            return None
        if c["backend"] == "tor":
            return {"scheme": "socks5", "host": "127.0.0.1", "port": TOR_SOCKS}
        return {"scheme": "socks5", "host": netns.NS_IP, "port": netns.SOCKS_PORT}

    # ------------------------------------------------------------ connect / disconnect
    def connect(self, backend: str, scope: str, place: str | None, hop: dict | None) -> dict:
        if backend not in BACKENDS:
            raise ApiError(f"unknown VPN {backend!r}")
        if scope not in SCOPES:
            raise ApiError("choose 'neox' (only NEOX) or 'system' (the whole computer)")
        hopper = None
        if hop and hop.get("minutes"):
            places = [str(p) for p in hop.get("places") or []]
            hopper = Hopper(float(hop["minutes"]), places, hop.get("order", "cycle"))
            place = place if place in places else places[0]
        if backend == "wireguard" and not place:
            items = self.wg.items()
            if not items:
                raise ApiError("import a WireGuard config first")
            place = items[0]["id"]
        with self.op:
            self._down()
            self.last_error = None
            self.conn = {"backend": backend, "scope": scope, "place": place, "state": "connecting",
                         "since": time.time(), "proxy": None}
            try:
                info = self._up(backend, scope, place)
            except ERRORS as exc:
                self._down()
                self.last_error = str(exc)
                raise ApiError(str(exc)) from exc
            self.conn.update(info, state="up", proxy=self.proxy())
            self.hopper = hopper
            log.info("connected: %s", self.conn)
            return self.status()

    def disconnect(self) -> dict:
        with self.op:
            self._down()
            self.last_error = None
        return self.status()

    def _up(self, backend: str, scope: str, place: str | None) -> dict:
        if backend == "riseup":
            return self._riseup_up(scope, place)
        if backend == "tor":
            self.tor.start(place, system=scope == "system")
            if scope == "system":
                system.tor_on()
            return {"country": (place or "").upper() or "any", "city": None, "server": "Tor network"}
        return self._wireguard_up(scope, place)

    def _down(self) -> None:
        self.hopper = None
        self._stop_openvpn()
        self.tor.stop()
        system.cleanup()
        if self.socks and self.socks.poll() is None:
            self.socks.kill()
        self.socks = None
        netns.destroy()
        self.conn = None

    # ------------------------------------------------------------ Riseup
    def _riseup_up(self, scope: str, place: str | None, exclude: str | None = None) -> dict:
        self.riseup.refresh()
        self.run.mkdir(parents=True, exist_ok=True)
        tried = set(exclude) if isinstance(exclude, (set, list, tuple)) else ({exclude} if exclude else set())
        for attempt in range(GATEWAY_TRIES):
            try:
                # a gateway we just left may still hold our old session; prefer another one in that city
                gw = self.riseup.pick(place, tried)
            except RiseupError:
                if attempt:
                    raise
                gw = self.riseup.pick(place)
            tried.add(gw.host)
            try:
                if scope == "neox":
                    self._riseup_neox(gw)
                else:
                    cfg = self._write(self.run / "neox-riseup.ovpn", system.system_openvpn_config(self.riseup.config(gw)))
                    try:
                        system.nm_import_up("openvpn", cfg)
                    finally:
                        cfg.unlink(missing_ok=True)
                    if not self._probe(in_netns=False):
                        system.nm_down()
                        raise ApiError(f"Riseup's gateway {gw.host.split('.')[0]} connected but reaches nothing")
                return {"country": gw.country, "city": gw.location, "server": gw.host}
            except (ApiError, system.SystemError_) as exc:
                log.warning("gateway %s failed: %s", gw.host, exc)
                last = exc
        raise last

    def _riseup_neox(self, gw) -> None:
        if not netns.exists():
            netns.create()
        if not self.socks or self.socks.poll() is not None:
            self.socks = netns.start_socks()
        cfg = self._write(self.run / "riseup.ovpn", self.riseup.config(gw, netns_up=UP_SCRIPT))
        self._stop_openvpn()
        logf = open(self.run / "openvpn.log", "w")
        self.openvpn = subprocess.Popen(["openvpn", "--config", str(cfg)], stdout=logf, stderr=subprocess.STDOUT,
                                        start_new_session=True, cwd=str(self.run))
        logf.close()
        try:
            self._wait_tunnel(lambda: self.openvpn.poll() is None, f"Riseup's gateway {gw.host.split('.')[0]}")
        except ApiError:
            self._stop_openvpn()
            raise

    def _stop_openvpn(self) -> None:
        p, self.openvpn = self.openvpn, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()

    def _wait_tunnel(self, alive, what: str) -> None:
        """Wait for the tunnel's route, then check that traffic really flows (a gateway can accept the
        connection yet pass nothing, e.g. while it still holds a stale session for our certificate)."""
        end = time.monotonic() + TUNNEL_TIMEOUT
        while time.monotonic() < end:
            if not alive():
                raise ApiError(f"could not connect to {what}{self._openvpn_reason()}")
            route = netns.sh("ip", "-n", netns.NS, "route", "show", "default", check=False).stdout.split()
            if route:
                if self._probe():
                    return
                raise ApiError(f"{what} connected but reaches nothing")
            time.sleep(0.2)
        raise ApiError(f"{what} did not answer within {TUNNEL_TIMEOUT} seconds")

    @staticmethod
    def _probe(in_netns: bool = True, tries: int = 3) -> bool:
        """Does the tunnel reach the internet? A gateway can accept us and answer pings yet pass
        nothing (one of Riseup's did), or still hold a stale session for our certificate. A TCP
        handshake with a public resolver on 443 (Quad9, else Cloudflare), through the tunnel."""
        code = ("import socket, sys\n"
                "for a in PROBES:\n"
                "    try:\n"
                "        socket.create_connection((a, 443), 4).close(); sys.exit(0)\n"
                "    except OSError:\n"
                "        pass\n"
                "sys.exit(1)").replace("PROBES", repr(PROBES))
        cmd = (["ip", "netns", "exec", netns.NS] if in_netns else []) + [sys.executable, "-I", "-c", code]
        for _ in range(tries):
            try:
                if spawn(cmd, timeout=20).returncode == 0:
                    return True
            except subprocess.TimeoutExpired:
                pass
            time.sleep(1)
        return False

    def _openvpn_reason(self) -> str:
        try:
            lines = (self.run / "openvpn.log").read_text(errors="replace").splitlines()
        except OSError:
            return ""
        bad = [l for l in lines if "ERROR" in l or "error" in l or "failed" in l.lower()]
        return f" ({bad[-1].split(' ', 5)[-1][:160]})" if bad else ""

    # ------------------------------------------------------------ WireGuard
    def _wireguard_up(self, scope: str, cid: str) -> dict:
        meta, c, text = self.wg.get(cid)
        if scope == "neox":
            if not netns.exists():
                netns.create(c.dns)
            if not self.socks or self.socks.poll() is not None:
                self.socks = netns.start_socks()
            netns.sh("ip", "-n", netns.NS, "link", "del", WG_DEV, check=False)
            netns.wireguard_up(WG_DEV, setconf(c), c.addresses, c.dns, c.mtu)
        else:
            # NetworkManager names the interface after the file
            cfg = self._write(self.run / f"{WG_DEV}.conf", text)
            try:
                system.nm_import_up("wireguard", cfg)
            finally:
                cfg.unlink(missing_ok=True)
        return {"country": meta.get("country") or None, "city": None, "server": meta.get("label"), "place": cid}

    def _restart_socks(self) -> None:
        """A new tunnel: NEOX's open connections still lead into the old one and would hang until
        they time out. Restarting the SOCKS server resets them, so NEOX reconnects at once."""
        if self.socks and self.socks.poll() is None:
            self.socks.kill()
            self.socks.wait(5)
        self.socks = netns.start_socks()

    @staticmethod
    def _write(path: Path, text: str) -> Path:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        return path

    # ------------------------------------------------------------ hopping and watching
    def hop(self) -> None:
        """Move to the next place on the list. Riseup and WireGuard reconnect (a few seconds with no
        connection; NEOX's traffic waits rather than leaking); Tor only changes its exit country."""
        with self.op:
            c, h = self.conn, self.hopper
            if not c or not h or c.get("state") not in ("up", "retrying"):
                return
            nxt = h.next(c["place"])
            log.info("hopping %s -> %s", c["place"], nxt)
            try:
                if c["backend"] == "tor":
                    self.tor.set_country(nxt)
                    info = {"country": nxt.upper(), "city": None, "server": "Tor network"}
                elif c["backend"] == "riseup":
                    c["state"] = "hopping"
                    if c["scope"] == "system":
                        system.nm_down()
                    info = self._riseup_up(c["scope"], nxt, exclude=c.get("server"))
                else:
                    c["state"] = "hopping"
                    if c["scope"] == "system":
                        system.nm_down()
                    info = self._wireguard_up(c["scope"], nxt)
                if c["scope"] == "neox" and c["backend"] != "tor":
                    self._restart_socks()
                c.update(info, place=nxt, state="up", hopped=time.time())
                self.last_error = None
            except ERRORS as exc:
                # stay on the list: try the next place in 30 seconds; NEOX-only traffic stays blocked meanwhile
                c["place"], c["state"] = nxt, "retrying"
                h.next_at = h.clock() + 30
                self.last_error = f"could not hop to {nxt}: {exc}"
                log.warning(self.last_error)

    def tick(self) -> None:
        c, h = self.conn, self.hopper
        if not c:
            return
        if h and h.due():
            self.hop()
            return
        # a dropped Riseup tunnel (NEOX only): reconnect to the same place
        if c["backend"] == "riseup" and c["scope"] == "neox" and c.get("state") == "up" \
                and self.openvpn is not None and self.openvpn.poll() is not None:
            with self.op:
                if self.conn is c:
                    c["state"] = "reconnecting"
                    try:
                        c.update(self._riseup_up("neox", c["place"], exclude=c.get("server")), state="up")
                        self._restart_socks()
                    except ERRORS as exc:
                        c["state"] = "retrying"
                        self.last_error = f"connection dropped: {exc}"
        # the SOCKS server is NEOX's only door into the namespace
        if c["scope"] == "neox" and c["backend"] != "tor" and self.socks is not None and self.socks.poll() is not None:
            with self.op:
                try:
                    self.socks = netns.start_socks()
                except netns.NetnsError as exc:
                    self.last_error = str(exc)

    def loop(self) -> None:
        while not self.stopping.wait(2):
            try:
                self.tick()
            except Exception:                       # the watcher must never die
                log.exception("tick failed")

    # ------------------------------------------------------------ exit IP
    def check_ip(self) -> dict:
        cmd = ["curl", "-sS", "--max-time", "20", "-H", "Accept: application/json"]
        p = self.proxy()
        if p:
            cmd += ["--socks5-hostname", f"{p['host']}:{p['port']}"]
        try:
            r = spawn(cmd + ["https://ifconfig.co/json"], timeout=30)
        except subprocess.TimeoutExpired:
            raise ApiError("the IP check timed out") from None
        if r.returncode != 0:
            raise ApiError(f"could not reach the IP check service ({r.stderr.strip()[:120] or r.returncode})")
        try:
            j = json.loads(r.stdout)
        except ValueError:
            raise ApiError("the IP check service gave an unreadable answer") from None
        return {"ip": j.get("ip"), "country": j.get("country_iso"), "city": j.get("city"), "through_vpn": bool(p) or
                bool(self.conn and self.conn.get("state") == "up")}

    # ------------------------------------------------------------ API
    def authorized(self, peer: tuple[int, int, int]) -> bool:
        pid, uid, _ = peer
        if uid == 0:
            return True
        try:
            return spawn(["pkcheck", "--action-id", POLKIT_ACTION, "--process", str(pid),
                          "--allow-user-interaction"], timeout=300).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def api(self, req: dict, peer: tuple[int, int, int]) -> dict:
        m, a = req.get("method"), req.get("params") or {}
        if m == "status":
            return self.status()
        if m == "providers":
            return self.providers()
        if m == "check_ip":
            return self.check_ip()
        if m in ("connect", "disconnect", "import_wireguard", "remove_wireguard"):
            if not self.authorized(peer):
                raise ApiError("not allowed: this needs the person at this computer to approve it")
            if m == "connect":
                return self.connect(a.get("backend", ""), a.get("scope", ""), a.get("place") or None, a.get("hop"))
            if m == "disconnect":
                return self.disconnect()
            if m == "import_wireguard":
                text = str(a.get("text", ""))
                if len(text) > 16384:
                    raise ApiError("that file is too big to be a WireGuard config")
                return {"imported": self.wg.add(str(a.get("filename", "wireguard.conf")), text,
                                                str(a.get("label", "")), str(a.get("country", "")))}
            return {"removed": self.wg.remove(str(a.get("id", "")))}
        raise ApiError(f"unknown method {m!r}")


# ---------------------------------------------------------------- the socket
def peercred(conn: socket.socket) -> tuple[int, int, int]:
    return struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))


def handle(vpn: VPN, conn: socket.socket) -> None:
    peer = peercred(conn)
    try:
        for line in conn.makefile("rb"):
            try:
                req = json.loads(line)
                out = {"ok": True, **vpn.api(req, peer)}
            except ValueError as exc:
                out = {"ok": False, "error": str(exc) if not isinstance(exc, json.JSONDecodeError) else "bad json"}
            except ERRORS as exc:
                out = {"ok": False, "error": str(exc)}
            except Exception as exc:               # never let one request take the service down
                log.exception("request failed")
                out = {"ok": False, "error": f"internal error: {exc}"}
            conn.sendall((json.dumps(out, default=str) + "\n").encode())
    except OSError:
        pass
    finally:
        conn.close()


def serve(vpn: VPN, path: Path = SOCKET) -> socket.socket:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o755)
    path.unlink(missing_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    os.chmod(path, 0o666)                          # anyone may connect; each request is judged by its caller
    srv.listen(16)

    def accept() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(vpn, conn), daemon=True).start()
    threading.Thread(target=accept, name="neox-vpn-api", daemon=True).start()
    return srv


def request(method: str, params: dict | None = None, path: Path = SOCKET, timeout: float = 180) -> dict:
    """Client side (used by neox-host and the tests)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(path))
        s.sendall((json.dumps({"method": method, "params": params or {}}) + "\n").encode())
        data = s.makefile("rb").readline()
    finally:
        s.close()
    if not data:
        raise ConnectionError("neox-vpnd closed the connection")
    return json.loads(data)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    vpn = VPN()
    vpn._down()                                    # nothing half-up from a previous run
    srv = serve(vpn)

    def stop(*_):
        vpn.stopping.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("neox-vpnd ready on %s", SOCKET)
    vpn.loop()
    srv.close()
    with vpn.op:
        vpn._down()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
