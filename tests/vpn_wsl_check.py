"""NEOX VPN for real, in the WSL dev system (run as root). Only private network namespaces are
touched: the dev system's own routing is never changed (whole-computer mode is checked in QEMU).

  1. Riseup, only NEOX: exit IP is Riseup's, hop to another city, a killed tunnel blocks (no leak),
     the watcher reconnects, and the machine's own exit IP never changes
  2. Tor, only NEOX: exit country as chosen, hop changes it
  3. WireGuard, only NEOX: two imported configs against a local test peer in a second namespace;
     hopping swaps configs; nothing reaches the internet outside the tunnel

    wsl -d Debian -u root -- python3 tests/vpn_wsl_check.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIB = Path("/tmp/nxlib")                     # the repo path has an apostrophe; nobody's python reads this copy
shutil.rmtree(LIB, ignore_errors=True)
shutil.copytree(REPO / "neox" / "vpn", LIB, ignore=shutil.ignore_patterns("__pycache__"))
(LIB / "neox-vpn-up").chmod(0o755)
LIB.chmod(0o755)
sys.path.insert(0, str(LIB))

from neox_vpn import daemon as D, netns  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)
    return ok


def sh(*cmd: str, check_: bool = True, **kw) -> subprocess.CompletedProcess:
    r = subprocess.run(list(cmd), capture_output=True, text=True, **kw)
    if check_ and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {r.stderr.strip()}")
    return r


def via_socks(proxy: dict, url: str, t: int = 20) -> subprocess.CompletedProcess:
    return sh("curl", "-sS", "--max-time", str(t), "--socks5-hostname", f"{proxy['host']}:{proxy['port']}", url,
              check_=False)


def direct_ip() -> dict:
    return json.loads(sh("curl", "-sS", "--max-time", "20", "-H", "Accept: application/json",
                         "https://ifconfig.co/json").stdout)


def riseup(vpn: D.VPN, home: dict) -> None:
    st = vpn.connect("riseup", "neox", "Paris", {"minutes": 1, "places": ["Paris", "Amsterdam"]})
    c = st["connection"]
    check("riseup: connected to Paris", st["connected"] and c["city"] == "Paris", c["server"])
    ip1 = vpn.check_ip()
    check("riseup: NEOX's exit is not the machine's", ip1["ip"] != home["ip"], f"{ip1} (Riseup says {c['city']})")
    vpn.hopper.next_at = 0
    vpn.tick()
    c = vpn.status()["connection"]
    ip2 = vpn.check_ip()
    check("riseup: hopped to Amsterdam", c["place"] == "Amsterdam" and c["state"] == "up" and ip2["ip"] != ip1["ip"],
          f"{c['server']} exit {ip2['ip']} (geolocated {ip2['country']}, Riseup says {c['city']} {c['country']})")
    vpn.openvpn.kill()
    vpn.openvpn.wait()
    time.sleep(0.5)
    r = via_socks(vpn.proxy(), "https://ifconfig.co/ip", 10)
    check("riseup: tunnel down -> NEOX's traffic stops (no leak)", r.returncode != 0, f"curl exit {r.returncode}")
    vpn.tick()
    c = vpn.status()["connection"]
    ip3 = vpn.check_ip()
    check("riseup: watcher reconnected", c["state"] == "up" and ip3["ip"] != home["ip"], f"{c['server']} {ip3['ip']}")
    check("riseup: the machine's own exit is unchanged", direct_ip()["ip"] == home["ip"])
    vpn.disconnect()
    check("riseup: disconnect removes the namespace", not netns.exists())


def tor(vpn: D.VPN) -> None:
    t0 = time.monotonic()
    st = vpn.connect("tor", "neox", "de", {"minutes": 1, "places": ["de", "nl"]})
    check("tor: connected", st["connected"], f"{time.monotonic() - t0:.0f}s to bootstrap")
    ip1 = vpn.check_ip()
    check("tor: exit country DE", ip1["country"] == "DE", str(ip1))
    vpn.hopper.next_at = 0
    vpn.tick()
    time.sleep(2)
    ip2 = vpn.check_ip()
    check("tor: hopped to NL", vpn.conn["place"] == "nl" and ip2["country"] == "NL", str(ip2))
    vpn.disconnect()
    check("tor: stopped", not vpn.tor.running())


PEER_NS, PEER_HOST, PEER_IP = "nxpeer", "nxp-host", "10.219.0.2"
HTTP = """
import http.server
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        b = ("peer sees " + self.client_address[0]).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass
http.server.HTTPServer(("10.77.0.1", 8080), H).serve_forever()
"""


def wireguard(vpn: D.VPN) -> None:
    key = lambda: sh("wg", "genkey").stdout.strip()
    pub = lambda k: sh("wg", "pubkey", input=k).stdout.strip()
    srv, c1, c2 = key(), key(), key()
    sh("ip", "netns", "del", PEER_NS, check_=False)
    sh("ip", "link", "del", PEER_HOST, check_=False)
    sh("ip", "netns", "add", PEER_NS)
    sh("ip", "link", "add", PEER_HOST, "type", "veth", "peer", "name", "nxp-ns")
    sh("ip", "link", "set", "nxp-ns", "netns", PEER_NS)
    sh("ip", "addr", "add", "10.219.0.1/30", "dev", PEER_HOST)
    sh("ip", "link", "set", PEER_HOST, "up")
    for cmd in (["addr", "add", f"{PEER_IP}/30", "dev", "nxp-ns"], ["link", "set", "nxp-ns", "up"],
                ["link", "set", "lo", "up"], ["link", "add", "wg0", "type", "wireguard"],
                ["addr", "add", "10.77.0.1/24", "dev", "wg0"]):
        sh("ip", "-n", PEER_NS, *cmd)
    Path("/tmp/nxpeer.key").write_text(srv)
    sh("ip", "netns", "exec", PEER_NS, "wg", "set", "wg0", "listen-port", "51820", "private-key", "/tmp/nxpeer.key",
       "peer", pub(c1), "allowed-ips", "10.77.0.2/32", "peer", pub(c2), "allowed-ips", "10.77.0.3/32")
    sh("ip", "-n", PEER_NS, "link", "set", "wg0", "up")
    web = subprocess.Popen(["ip", "netns", "exec", PEER_NS, "python3", "-c", HTTP])
    try:
        for name, k, addr in (("DE-peer-one.conf", c1, "10.77.0.2/32"), ("NL-peer-two.conf", c2, "10.77.0.3/32")):
            vpn.wg.add(name, f"[Interface]\nPrivateKey = {k}\nAddress = {addr}\nDNS = 10.77.0.1\n\n[Peer]\n"
                             f"PublicKey = {pub(srv)}\nAllowedIPs = 0.0.0.0/0\nEndpoint = {PEER_IP}:51820\n")
        ids = [i["id"] for i in vpn.wg.items()]
        st = vpn.connect("wireguard", "neox", ids[0], {"minutes": 1, "places": ids})
        time.sleep(1)
        r = via_socks(vpn.proxy(), "http://10.77.0.1:8080/")
        check("wireguard: NEOX reaches the peer through the tunnel", r.stdout == "peer sees 10.77.0.2",
              f"{st['connection']['country']} {r.stdout or r.stderr.strip()}")
        r = via_socks(vpn.proxy(), "http://1.1.1.1/", 8)
        check("wireguard: nothing goes around the tunnel", r.returncode != 0, f"curl exit {r.returncode}")
        vpn.hopper.next_at = 0
        vpn.tick()
        time.sleep(1)
        r = via_socks(vpn.proxy(), "http://10.77.0.1:8080/")
        check("wireguard: hopped to the second config", r.stdout == "peer sees 10.77.0.3"
              and vpn.conn["country"] == "NL", r.stdout or r.stderr.strip())
        check("wireguard: the tunnel's UDP stays outside, the interface inside",
              "nxwg0" in sh("ip", "-n", netns.NS, "link").stdout and "nxwg0" not in sh("ip", "link").stdout)
    finally:
        vpn.disconnect()
        web.kill()
        for i in vpn.wg.items():
            vpn.wg.remove(i["id"])
        sh("ip", "netns", "del", PEER_NS, check_=False)
        sh("ip", "link", "del", PEER_HOST, check_=False)
        Path("/tmp/nxpeer.key").unlink(missing_ok=True)


def main() -> int:
    state = Path("/tmp/nxvpn-state")
    shutil.rmtree(state, ignore_errors=True)
    vpn = D.VPN(state, Path("/tmp/nxvpn-run"))
    home = direct_ip()
    print(f"this machine's own exit: {home.get('ip')} {home.get('country_iso')}", flush=True)
    which = sys.argv[1:] or ["riseup", "tor", "wireguard"]
    try:
        if "riseup" in which:
            riseup(vpn, home)
        if "tor" in which:
            tor(vpn)
        if "wireguard" in which:
            wireguard(vpn)
    except Exception as exc:
        check("no unexpected error", False, repr(exc))
    finally:
        vpn._down()
    bad = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed", flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
