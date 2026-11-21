"""TLS certificate, paired devices, pairing codes, LAN-only addresses and failure rate limits."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import secrets
import ssl
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .config import private_write, state_dir

PAIR_SECONDS = 120


# ---------------------------------------------------------------- the PC's certificate
def ensure_cert(d: Path | None = None) -> tuple[Path, Path, str]:
    """A self-signed ECDSA P-256 certificate made once with openssl. Returns (cert, key, SHA-256 of its
    DER bytes as lowercase hex): the phone pins exactly this fingerprint (it comes in the QR code)."""
    d = d or state_dir()
    cert, key = d / "cert.pem", d / "key.pem"
    if not (cert.exists() and key.exists()):
        tmpkey = d / "key.pem.new"
        old = os.umask(0o077)
        try:
            r = subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                                "-nodes", "-days", "3650", "-subj", "/CN=AIROX Connect", "-keyout", str(tmpkey),
                                "-out", str(cert)], capture_output=True, text=True, timeout=60)
        finally:
            os.umask(old)
        if r.returncode != 0:
            raise RuntimeError(f"could not make the certificate: {r.stderr.strip()[-300:]}")
        os.chmod(tmpkey, 0o600)
        os.replace(tmpkey, key)
    return cert, key, fingerprint(cert)


def fingerprint(cert: Path) -> str:
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()


def server_context(cert: Path, key: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(str(cert), str(key))
    return ctx


# ---------------------------------------------------------------- LAN only
def lan_ok(host: str) -> bool:
    """Private (RFC 1918, ULA), link-local or loopback addresses only. Carrier-grade NAT space
    (100.64.0.0/10, also used by tunnels) and public addresses are refused."""
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip in ipaddress.ip_network("100.64.0.0/10"):
        return False
    return ip.is_loopback or ip.is_link_local or (ip.is_private and not ip.is_unspecified and not ip.is_reserved)


def lan_addresses() -> list[str]:
    """This PC's IPv4 LAN addresses (for the QR code), from `ip -j addr`."""
    try:
        r = subprocess.run(["ip", "-j", "-4", "addr", "show", "scope", "global"], capture_output=True, text=True, timeout=5)
        data = json.loads(r.stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    out = []
    for link in data:
        name = link.get("ifname", "")
        if name.startswith(("docker", "podman", "veth", "virbr", "br-", "cni", "neoxvpn", "tun", "wg")):
            continue
        for a in link.get("addr_info", []):
            if a.get("family") == "inet" and lan_ok(a.get("local", "")) and not a["local"].startswith("127."):
                out.append(a["local"])
    return out


def mac_addresses() -> list[str]:
    """MAC addresses of physical network interfaces, for Wake-on-LAN from the phone."""
    out = []
    for d in sorted(Path("/sys/class/net").glob("*")):
        if not (d / "device").exists():
            continue
        try:
            mac = (d / "address").read_text().strip()
        except OSError:
            continue
        if mac and mac != "00:00:00:00:00:00":
            out.append(mac)
    return out


# ---------------------------------------------------------------- paired devices
def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Devices:
    """devices.json (0600): the PC's id and each paired device, with only the SHA-256 of its token."""

    def __init__(self, path: Path | None = None):
        self.path = path or state_dir() / "devices.json"
        self.lock = threading.Lock()
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}
        if "pc_id" not in self.data:
            self.data = {"pc_id": secrets.token_hex(8), "devices": {}}
            self._save()

    @property
    def pc_id(self) -> str:
        return self.data["pc_id"]

    def _save(self) -> None:
        private_write(self.path, json.dumps(self.data, indent=1))

    def add(self, name: str, model: str = "", platform: str = "android") -> tuple[str, str]:
        dev_id, token = uuid.uuid4().hex[:16], secrets.token_urlsafe(32)
        with self.lock:
            self.data["devices"][dev_id] = {"name": name[:60] or "Phone", "model": model[:80], "platform": platform[:20],
                                            "token_sha256": _h(token), "paired": time.time(), "last_seen": None,
                                            "last_ip": None}
            self._save()
        return dev_id, token

    def verify(self, auth: str) -> dict | None:
        """`Bearer <device id>.<token>` -> the device (with its id), or None."""
        if not auth.startswith("Bearer "):
            return None
        dev_id, _, token = auth[7:].strip().partition(".")
        with self.lock:
            d = self.data["devices"].get(dev_id)
        if d is None or not token or not secrets.compare_digest(d["token_sha256"], _h(token)):
            return None
        return {"id": dev_id, **d}

    def seen(self, dev_id: str, ip: str) -> None:
        with self.lock:
            d = self.data["devices"].get(dev_id)
            if d is not None and (d.get("last_ip") != ip or time.time() - (d.get("last_seen") or 0) > 60):
                d["last_seen"], d["last_ip"] = time.time(), ip
                self._save()

    def revoke(self, dev_id: str) -> bool:
        with self.lock:
            gone = self.data["devices"].pop(dev_id, None) is not None
            if gone:
                self._save()
        return gone

    def get(self, dev_id: str) -> dict | None:
        with self.lock:
            d = self.data["devices"].get(dev_id)
            return {"id": dev_id, **d} if d else None

    def list(self) -> list[dict]:
        with self.lock:
            return [{"id": k, **{x: v[x] for x in ("name", "model", "platform", "paired", "last_seen", "last_ip")}}
                    for k, v in self.data["devices"].items()]


# ---------------------------------------------------------------- pairing
class Pairing:
    """One-time pairing codes (128-bit, 120 s, single use) and the PC-side approval of each request."""

    def __init__(self, seconds: float = PAIR_SECONDS):
        self.seconds = seconds
        self.code: str | None = None
        self.expires = 0.0
        self.pending: dict[str, dict] = {}
        self.lock = threading.Lock()

    def new_code(self) -> tuple[str, float]:
        with self.lock:
            self.code, self.expires = secrets.token_hex(16), time.time() + self.seconds
            return self.code, self.expires

    def cancel(self) -> None:
        with self.lock:
            self.code = None

    def active(self) -> bool:
        with self.lock:
            return self.code is not None and time.time() < self.expires

    def claim(self, code: str) -> bool:
        with self.lock:
            ok = (self.code is not None and time.time() < self.expires and isinstance(code, str)
                  and secrets.compare_digest(code.encode(), self.code.encode()))
            if ok:
                self.code = None            # single use
            return ok

    def request(self, info: dict) -> dict:
        rid = secrets.token_hex(6)
        p = {"id": rid, **info, "event": threading.Event(), "answer": None, "time": time.time()}
        with self.lock:
            self.pending[rid] = p
        return p

    def answer(self, rid: str, yes: bool) -> bool:
        with self.lock:
            p = self.pending.get(rid)
        if p is None:
            return False
        p["answer"] = bool(yes)
        p["event"].set()
        return True

    def wait(self, p: dict, timeout: float) -> bool:
        p["event"].wait(timeout)
        with self.lock:
            self.pending.pop(p["id"], None)
        return bool(p["answer"])

    def requests(self) -> list[dict]:
        with self.lock:
            return [{k: v for k, v in p.items() if k not in ("event", "answer")} for p in self.pending.values()]


class Limiter:
    """At most `n` failures per address per `window` seconds."""

    def __init__(self, n: int = 10, window: float = 60.0):
        self.n, self.window = n, window
        self.fails: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def blocked(self, ip: str) -> bool:
        now = time.time()
        with self.lock:
            f = [t for t in self.fails.get(ip, []) if now - t < self.window]
            self.fails[ip] = f
            return len(f) >= self.n

    def fail(self, ip: str) -> None:
        with self.lock:
            self.fails.setdefault(ip, []).append(time.time())
