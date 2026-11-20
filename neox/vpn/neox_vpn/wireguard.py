"""Bring your own WireGuard: configs from a provider you signed up to (e.g. Proton VPN's free plan).

Imported .conf files are checked, then stored root-only with a label and a country (given by the
user, or taken from a provider file name like "US-FREE#12.conf"). Hopping rotates through them.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import re
from dataclasses import dataclass
from pathlib import Path

INTERFACE_KEYS = {"PrivateKey", "Address", "DNS", "MTU", "ListenPort"}
PEER_KEYS = {"PublicKey", "PresharedKey", "Endpoint", "AllowedIPs", "PersistentKeepalive"}


class WireGuardError(ValueError):
    pass


@dataclass
class WgConfig:
    private_key: str
    addresses: list[str]
    dns: list[str]
    mtu: int | None
    peers: list[dict]


def _key(v: str, what: str) -> str:
    try:
        if len(base64.b64decode(v, validate=True)) != 32:
            raise ValueError
    except ValueError:
        raise WireGuardError(f"{what} is not a WireGuard key") from None
    return v


def parse(text: str) -> WgConfig:
    section, iface, peers = None, {}, []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("["):
            section = line.strip("[]").strip().lower()
            if section == "peer":
                peers.append({})
            elif section != "interface":
                raise WireGuardError(f"unknown section [{section}]")
            continue
        k, sep, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if not sep or section is None:
            raise WireGuardError(f"not a WireGuard config line: {raw.strip()[:40]}")
        if section == "interface":
            if k not in INTERFACE_KEYS and k not in ("PostUp", "PostDown", "PreUp", "PreDown", "Table", "SaveConfig"):
                raise WireGuardError(f"unknown setting {k}")
            if k in ("PostUp", "PostDown", "PreUp", "PreDown"):
                raise WireGuardError(f"{k} runs commands as root; NEOX VPN does not accept it")
            iface[k] = v
        else:
            if k not in PEER_KEYS:
                raise WireGuardError(f"unknown peer setting {k}")
            peers[-1][k] = v
    if "PrivateKey" not in iface:
        raise WireGuardError("no PrivateKey in [Interface]")
    if not peers:
        raise WireGuardError("no [Peer]")
    _key(iface["PrivateKey"], "PrivateKey")
    addrs = [a.strip() for a in iface.get("Address", "").split(",") if a.strip()]
    if not addrs:
        raise WireGuardError("no Address in [Interface]")
    for a in addrs:
        ipaddress.ip_interface(a)
    dns = [d.strip() for d in iface.get("DNS", "").split(",") if d.strip()]
    for d in dns:
        ipaddress.ip_address(d)
    for p in peers:
        _key(p.get("PublicKey", ""), "PublicKey")
        if "PresharedKey" in p:
            _key(p["PresharedKey"], "PresharedKey")
        ep = p.get("Endpoint", "")
        if not re.fullmatch(r"(\[[0-9a-fA-F:]+\]|[\w.\-]+):\d{1,5}", ep):
            raise WireGuardError(f"bad Endpoint {ep!r}")
        for n in p.get("AllowedIPs", "0.0.0.0/0").split(","):
            ipaddress.ip_network(n.strip(), strict=False)
    return WgConfig(iface["PrivateKey"], addrs, dns, int(iface["MTU"]) if iface.get("MTU") else None, peers)


def setconf(c: WgConfig) -> str:
    """What `wg setconf` accepts (wg-quick's Address/DNS/MTU are applied separately)."""
    out = ["[Interface]", f"PrivateKey = {c.private_key}"]
    for p in c.peers:
        out.append("[Peer]")
        out += [f"{k} = {v}" for k, v in p.items()]
    return "\n".join(out) + "\n"


def country_from_name(name: str) -> str:
    m = re.match(r"^([A-Za-z]{2})(?:[-_#\s]|$)", Path(name).stem)
    return m.group(1).upper() if m else ""


class Store:
    def __init__(self, root: Path):
        self.root = root

    def _meta(self) -> dict:
        p = self.root / "index.json"
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return {}

    def items(self) -> list[dict]:
        return [{"id": k, **v} for k, v in sorted(self._meta().items())]

    def add(self, filename: str, text: str, label: str = "", country: str = "") -> dict:
        parse(text)                                     # refuse anything that is not a clean config
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        slug = re.sub(r"[^a-z0-9]+", "-", (label or Path(filename).stem).lower()).strip("-")[:24] or "wg"
        meta = self._meta()
        base, n = slug, 2
        while slug in meta:
            slug, n = f"{base}-{n}", n + 1
        (self.root / f"{slug}.conf").write_text(text)
        (self.root / f"{slug}.conf").chmod(0o600)
        meta[slug] = {"label": label or Path(filename).stem, "country": (country or country_from_name(filename)).upper()}
        (self.root / "index.json").write_text(json.dumps(meta))
        return {"id": slug, **meta[slug]}

    def remove(self, cid: str) -> bool:
        meta = self._meta()
        if cid not in meta:
            return False
        del meta[cid]
        (self.root / f"{cid}.conf").unlink(missing_ok=True)
        (self.root / "index.json").write_text(json.dumps(meta))
        return True

    def get(self, cid: str) -> tuple[dict, WgConfig, str]:
        meta = self._meta()
        if cid not in meta:
            raise WireGuardError(f"no WireGuard config {cid}")
        text = (self.root / f"{cid}.conf").read_text()
        return {"id": cid, **meta[cid]}, parse(text), text
