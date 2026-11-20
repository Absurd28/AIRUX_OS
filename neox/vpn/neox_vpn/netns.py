""""Only NEOX": the tunnel lives in its own network namespace, reached through a SOCKS5 server.

  main namespace   nx-host 10.218.0.1/30 ──veth── nx-ns 10.218.0.2/30   namespace "neoxvpn"
                   (the tunnel's outer UDP/TCP socket stays here)        tun / wg interface, default
                                                                         route, DNS, SOCKS on :1080
The tunnel interface is created in the main namespace and then moved: OpenVPN and WireGuard keep
using it from there, while only what runs inside the namespace (the SOCKS server) is routed through
it. With no tunnel, the namespace has no default route, so NEOX's traffic stops rather than leaking.
"""
from __future__ import annotations

import ipaddress
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

NS = "neoxvpn"
HOST_IF, NS_IF = "nx-host", "nx-ns"
HOST_IP, NS_IP = "10.218.0.1", "10.218.0.2"
SOCKS_PORT = 1080
RESOLV = Path("/etc/netns") / NS / "resolv.conf"
LIB = Path(__file__).resolve().parent.parent       # where the neox_vpn package lives


class NetnsError(Exception):
    pass


def sh(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(list(cmd), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise NetnsError(f"{' '.join(cmd)}: {r.stderr.strip() or r.stdout.strip()}")
    return r


def exists() -> bool:
    return NS in sh("ip", "netns", "list", check=False).stdout.split()


def create(dns: list[str] | None = None) -> None:
    destroy()
    sh("ip", "netns", "add", NS)
    sh("ip", "link", "add", HOST_IF, "type", "veth", "peer", "name", NS_IF)
    sh("ip", "link", "set", NS_IF, "netns", NS)
    sh("ip", "addr", "add", f"{HOST_IP}/30", "dev", HOST_IF)
    sh("ip", "link", "set", HOST_IF, "up")
    sh("ip", "-n", NS, "addr", "add", f"{NS_IP}/30", "dev", NS_IF)
    sh("ip", "-n", NS, "link", "set", NS_IF, "up")
    sh("ip", "-n", NS, "link", "set", "lo", "up")
    set_dns(dns or [])


def set_dns(dns: list[str]) -> None:
    RESOLV.parent.mkdir(parents=True, exist_ok=True)
    RESOLV.write_text("".join(f"nameserver {d}\n" for d in dns) or "nameserver 9.9.9.9\n")
    RESOLV.chmod(0o644)


def destroy() -> None:
    if exists():
        sh("ip", "netns", "del", NS, check=False)
    sh("ip", "link", "del", HOST_IF, check=False)
    shutil.rmtree(RESOLV.parent, ignore_errors=True)


def adopt(dev: str, address: str, gateway: str | None = None, mtu: int | None = None) -> None:
    """Move a tunnel interface into the namespace and route everything there through it."""
    sh("ip", "link", "set", dev, "netns", NS)
    sh("ip", "-n", NS, "addr", "add", address, "dev", dev)
    if mtu:
        sh("ip", "-n", NS, "link", "set", dev, "mtu", str(mtu))
    sh("ip", "-n", NS, "link", "set", dev, "up")
    if gateway:
        sh("ip", "-n", NS, "route", "replace", "default", "via", gateway, "dev", dev)
    else:
        sh("ip", "-n", NS, "route", "replace", "default", "dev", dev)


def start_socks() -> subprocess.Popen:
    """The SOCKS server inside the namespace, as an unprivileged user."""
    code = f"import sys; sys.path.insert(0, {str(LIB)!r}); from neox_vpn.socks import main; main(['{NS_IP}', '{SOCKS_PORT}'])"
    p = subprocess.Popen(["ip", "netns", "exec", NS, "setpriv", "--reuid=nobody", "--regid=nogroup", "--clear-groups",
                          sys.executable, "-I", "-c", code],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(50):
        if p.poll() is not None:
            raise NetnsError("the SOCKS server did not start")
        if sh("ip", "netns", "exec", NS, "ss", "-ltn", check=False).stdout.find(f"{NS_IP}:{SOCKS_PORT}") >= 0:
            return p
        time.sleep(0.1)
    p.kill()
    raise NetnsError("the SOCKS server did not start listening")


def wireguard_up(dev: str, setconf_text: str, addresses: list[str], dns: list[str], mtu: int | None) -> None:
    """Create a WireGuard interface here (its UDP socket stays in the main namespace), then move it."""
    sh("ip", "link", "del", dev, check=False)
    sh("ip", "link", "add", dev, "type", "wireguard")
    conf = Path(f"/run/neox-vpn/{dev}.setconf")
    conf.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(conf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(setconf_text)
    try:
        sh("wg", "setconf", dev, str(conf))
    finally:
        conf.unlink(missing_ok=True)
    v4 = [a for a in addresses if ipaddress.ip_interface(a).version == 4]
    adopt(dev, v4[0] if v4 else addresses[0], None, mtu)
    for a in addresses:
        if a != (v4[0] if v4 else addresses[0]):
            sh("ip", "-n", NS, "addr", "add", a, "dev", dev, check=False)
    set_dns(dns)


def openvpn_up_env(env: dict) -> None:
    """Called by OpenVPN's up-script (see bin/neox-vpn-up): move the tun in, set address, route, DNS."""
    dev = env["dev"]
    local = env["ifconfig_local"]
    mask = env.get("ifconfig_netmask")
    prefix = ipaddress.ip_network(f"0.0.0.0/{mask}").prefixlen if mask else 32
    gw = env.get("route_vpn_gateway") or env.get("ifconfig_remote")
    adopt(dev, f"{local}/{prefix}", gw, int(env["tun_mtu"]) if env.get("tun_mtu") else None)
    dns = []
    i = 1
    while f"foreign_option_{i}" in env:
        parts = env[f"foreign_option_{i}"].split()
        if len(parts) == 3 and parts[0] == "dhcp-option" and parts[1] == "DNS":
            dns.append(parts[2])
        i += 1
    set_dns(dns or ([gw] if gw else []))
