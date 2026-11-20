"""RiseupVPN through Riseup's open LEAP API: free, anonymous, no account.

provider.json -> the CA certificate (checked against the published SHA-256 fingerprint) ->
eip-service.json (gateways and their cities) -> an anonymous client certificate -> one OpenVPN
config per gateway. Every HTTPS call after the first is verified with Riseup's own CA.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import ssl
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path

PROVIDER = "https://black.riseup.net/provider.json"
CA_FINGERPRINT = "825fe0695d0134e7b7c80dd624cf33780d8e5e7fbd782ca0c8a87e24e108b468"   # provider.json, 2026-11


class RiseupError(Exception):
    pass


@dataclass
class Gateway:
    host: str
    ip: str
    location: str           # city
    country: str            # ISO code
    ports: list[str]
    protocols: list[str]


def _get(url: str, ctx: ssl.SSLContext | None = None, timeout: float = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "NEOX-VPN/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.read()
    except OSError as exc:
        raise RiseupError(f"cannot reach Riseup ({url.split('/')[2]}): {exc}") from exc


def pem_fingerprint(pem: str) -> str:
    der = ssl.PEM_cert_to_DER_cert(pem.strip() + "\n" if "-----END" in pem else pem)
    return hashlib.sha256(der).hexdigest()


def parse_gateways(eip: dict) -> list[Gateway]:
    locs = eip.get("locations", {})
    out = []
    for g in eip.get("gateways", []):
        for t in g.get("capabilities", {}).get("transport", []):
            if t.get("type") == "openvpn":
                loc = g.get("location", "")
                out.append(Gateway(g["host"], g["ip_address"], locs.get(loc, {}).get("name", loc),
                                   locs.get(loc, {}).get("country_code", "").upper(), t.get("ports", []),
                                   t.get("protocols", [])))
    return out


def openvpn_config(gw: Gateway, opts: dict, ca: str, cert_pem: str, netns_up: str | None = None) -> str:
    """An OpenVPN client config for one gateway, with Riseup's own options. In "only NEOX" mode the
    up-script moves the tunnel into the namespace and nothing is routed in the main one."""
    proto = "udp" if "udp" in gw.protocols else "tcp"
    port = "1194" if "1194" in gw.ports else gw.ports[0]
    lines = ["client", "tls-client", f"remote {gw.ip} {port} {proto}", "remote-cert-tls server",
             # the gateways' certificate names (CN) are the short host names, e.g. vpn03-par
             f"verify-x509-name {gw.host.split('.')[0]} name", "dev tun", "nobind", "persist-key", "auth-nocache",
             f"cipher {opts.get('cipher', 'AES-256-GCM')}", f"data-ciphers {opts.get('data-ciphers', 'AES-256-GCM')}",
             f"auth {opts.get('auth', 'SHA512')}", f"tls-version-min {opts.get('tls-version-min', '1.2')}",
             f"keepalive {opts.get('keepalive', '10 30')}", "verb 3",
             "<ca>", ca.strip(), "</ca>"]
    key = re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", cert_pem, re.S)
    cert = re.search(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", cert_pem, re.S)
    if not key or not cert:
        raise RiseupError("the client certificate is incomplete")
    lines += ["<key>", key.group(0), "</key>", "<cert>", cert.group(0), "</cert>"]
    if proto == "udp":
        # tell the gateway when we leave: over UDP it otherwise keeps our old session (and our tunnel
        # address) for a minute or more, and a new session from the same certificate stops working
        lines.append("explicit-exit-notify 1")
    if netns_up:
        lines += ["ifconfig-noexec", "route-noexec", "script-security 2", f"up {netns_up}", "up-restart"]
    return "\n".join(lines) + "\n"


class Riseup:
    def __init__(self, state: Path):
        self.state = state
        self.state.mkdir(parents=True, exist_ok=True)
        self.state.chmod(0o700)                    # the anonymous client key is kept here
        self.eip: dict | None = None
        self.ca: str | None = None
        self.api: str | None = None

    def _ctx(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context(cafile=None)
        ctx.load_verify_locations(cadata=self.ca)
        return ctx

    def refresh(self) -> None:
        provider = json.loads(_get(PROVIDER))
        if not provider.get("service", {}).get("allow_anonymous"):
            raise RiseupError("Riseup no longer offers anonymous access")
        want = provider.get("ca_cert_fingerprint", "").split(":")[-1].strip().lower()
        ca = _get(provider["ca_cert_uri"]).decode()
        got = pem_fingerprint(ca)
        if got != CA_FINGERPRINT or want != CA_FINGERPRINT:
            raise RiseupError("Riseup's CA certificate does not match the pinned fingerprint; not connecting")
        self.ca = ca
        self.api = provider["api_uri"].rstrip("/") + "/" + provider.get("api_version", "3")
        self.eip = json.loads(_get(self.api + "/config/eip-service.json", self._ctx()))
        (self.state / "eip-service.json").write_text(json.dumps(self.eip))
        (self.state / "ca.crt").write_text(ca)

    def gateways(self) -> list[Gateway]:
        if self.eip is None:
            self.refresh()
        return parse_gateways(self.eip)

    def locations(self) -> list[dict]:
        seen = {}
        for g in self.gateways():
            seen.setdefault(g.location, {"id": g.location, "city": g.location, "country": g.country})
        return sorted(seen.values(), key=lambda x: (x["country"], x["city"]))

    def certificate(self) -> str:
        """The anonymous client certificate (+ key); fetched again when under a day from expiry."""
        p = self.state / "client.pem"
        if p.exists():
            r = subprocess.run(["openssl", "x509", "-noout", "-checkend", "86400"], input=p.read_text(),
                               capture_output=True, text=True)
            if r.returncode == 0:
                return p.read_text()
        if self.ca is None:
            self.refresh()
        pem = _get(self.api + "/cert", self._ctx()).decode()
        if "BEGIN CERTIFICATE" not in pem or "PRIVATE KEY" not in pem:
            raise RiseupError("Riseup did not return a client certificate")
        p.write_text(pem)
        p.chmod(0o600)
        return pem

    def pick(self, location: str | None, exclude: str | set | None = None, rng=random) -> Gateway:
        skip = {exclude} if isinstance(exclude, str) else (exclude or set())
        gws = [g for g in self.gateways() if (not location or g.location == location or g.country == location)
               and g.host not in skip]
        if not gws:
            raise RiseupError(f"Riseup has no gateway in {location}")
        return rng.choice(gws)

    def config(self, gw: Gateway, netns_up: str | None = None) -> str:
        return openvpn_config(gw, self.eip.get("openvpn_configuration", {}), self.ca, self.certificate(), netns_up)
