""""Whole computer": the VPN carries all of AIRUX's traffic.

  Riseup, WireGuard  a NetworkManager connection named "NEOX VPN" (routes and DNS done properly,
                     shown in Plasma's network applet); OpenVPN needs network-manager-openvpn
  Tor                transparent proxy: an nftables table sends every TCP connection to Tor's
                     TransPort and DNS to its DNSPort; other UDP and IPv6 are dropped (no leaks);
                     LAN and loopback stay direct. The table is removed on disconnect and at start.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .tor import DNS_PORT, TRANS_PORT, VIRTUAL_NET

NM_ID = "NEOX VPN"
TABLE = "neox_vpn"
# local networks stay direct (and DHCP and multicast, which never leave the LAN)
LAN = "127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16, 100.64.0.0/10, 224.0.0.0/4, 255.255.255.255"


class SystemError_(Exception):
    pass


def _run(cmd: list[str], timeout: float = 60, input_text: str | None = None) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input_text)
    if r.returncode != 0:
        raise SystemError_(f"{' '.join(cmd[:3])}: {(r.stderr or r.stdout).strip()[:300]}")
    return r


# ---------------------------------------------------------------- NetworkManager
def _quiet(cmd: list[str], timeout: float) -> None:
    try:
        subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):      # not installed, or hung: nothing to undo then
        pass


def nm_down() -> None:
    _quiet(["nmcli", "connection", "delete", "id", NM_ID], 30)


def nm_import_up(kind: str, path: Path, timeout: float = 60) -> None:
    """Import an OpenVPN or WireGuard file as the "NEOX VPN" connection and bring it up."""
    nm_down()
    out = _run(["nmcli", "connection", "import", "type", kind, "file", str(path)]).stdout
    # "Connection 'neox-riseup' (uuid) successfully added."
    uuid = out.split("(")[1].split(")")[0] if "(" in out else None
    ref = ["uuid", uuid] if uuid else ["id", path.stem]
    _run(["nmcli", "connection", "modify", *ref, "connection.id", NM_ID, "connection.autoconnect", "no"])
    _run(["nmcli", "--wait", str(int(timeout)), "connection", "up", "id", NM_ID], timeout=timeout + 10)


def system_openvpn_config(cfg: str) -> str:
    """Riseup's config without the options NetworkManager's OpenVPN plugin does not take."""
    drop = ("auth-nocache", "tls-client", "persist-key", "nobind", "verb ")
    return "\n".join(l for l in cfg.splitlines() if not l.startswith(drop)) + "\n"


# ---------------------------------------------------------------- Tor transparent proxy
def tor_rules(tor_user: str = "debian-tor") -> str:
    return f"""table inet {TABLE} {{
  chain nat_out {{
    type nat hook output priority -100; policy accept;
    meta skuid "{tor_user}" return
    oifname "lo" return
    ip daddr {VIRTUAL_NET} meta l4proto tcp redirect to :{TRANS_PORT}
    udp dport 53 redirect to :{DNS_PORT}
    tcp dport 53 redirect to :{DNS_PORT}
    ip daddr {{ {LAN} }} return
    meta nfproto ipv4 meta l4proto tcp redirect to :{TRANS_PORT}
  }}
  chain filter_out {{
    type filter hook output priority 0; policy accept;
    meta skuid "{tor_user}" accept
    oifname "lo" accept
    ct state established,related accept
    ip daddr {{ {LAN} }} accept
    meta nfproto ipv6 drop
    meta l4proto udp drop
  }}
}}
"""


def tor_on() -> None:
    tor_off()
    _run(["nft", "-f", "-"], input_text=tor_rules())


def tor_off() -> None:
    _quiet(["nft", "delete", "table", "inet", TABLE], 15)


def cleanup() -> None:
    """Leftovers from a previous run (phagosd-style: never leave the network in a half state)."""
    tor_off()
    nm_down()
