#!/usr/bin/env python3
"""NEOX VPN on the live ISO in QEMU (run as root). The VPN tab of NEOX's side panel is driven for
real (through Chromium's DevTools protocol), so every step goes panel -> NEOX's extension -> native
host -> neox-vpnd (polkit, as the desktop user). Pass requires:

  service       neox-vpnd is running with its packages; the user can ask for its status
  neox_only     Riseup "only NEOX": NEOX's exit IP is Riseup's, the system's own is unchanged
  hop           "change country every 1 minute": the tunnel moves from Paris to Amsterdam by itself,
                NEOX's exit IP changes, the panel shows the new place
  kill_switch   the service stops: NEOX's pages stop loading (nothing leaks); "Turn the VPN off"
                gives NEOX its normal connection back
  system_riseup Riseup "whole computer": the system's exit IP changes (NetworkManager's "NEOX VPN");
                disconnecting restores it
  system_tor    Tor "whole computer", exit country DE: the system's exit is a German Tor exit, names
                still resolve; disconnecting restores it and removes the firewall table
  screenshot    the VPN tab, connected
WireGuard is checked in WSL against a local peer (tests/vpn_wsl_check.py): QEMU has no peer.
"""
from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
import desktop_check as dc  # noqa: E402

vm, D, sh, p8 = dc.vm, dc.D, dc.sh, dc.p8
USER = dc.USER
SHOTS = REPO / "docs" / "screenshots"
PORT = 9229
EXT = "caeflmcomdjlfjeinfhpookmojmhiphd"

# a tiny DevTools client (standard library only): list targets, evaluate JavaScript, navigate
CDP_PY = r'''
import base64, json, os, socket, struct, sys, urllib.request
PORT = int(os.environ.get("CDP_PORT", "9229"))
def targets():
    return json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=10))
def find(match):
    for t in targets():
        if t.get("id") == match or match in t.get("url", ""):
            return t
    raise SystemExit(json.dumps({"error": f"no target {match}"}))
def rx(s, n):
    b = b""
    while len(b) < n:
        c = s.recv(n - len(b))
        if not c: raise ConnectionError("closed")
        b += c
    return b
def call(t, method, params):
    hostport, path = t["webSocketDebuggerUrl"][5:].split("/", 1)
    s = socket.create_connection(("127.0.0.1", PORT), timeout=90)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    head = b""
    while b"\r\n\r\n" not in head: head += s.recv(1)
    assert b" 101 " in head.split(b"\r\n")[0], head[:80]
    data = json.dumps({"id": 1, "method": method, "params": params}).encode()
    mask, n = os.urandom(4), len(data)
    hdr = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack("!H", n) if n < 65536
                           else bytes([0x80 | 127]) + struct.pack("!Q", n))
    s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
    buf = b""
    while True:
        b1, b2 = rx(s, 2)
        n = b2 & 0x7f
        if n == 126: n = struct.unpack("!H", rx(s, 2))[0]
        elif n == 127: n = struct.unpack("!Q", rx(s, 8))[0]
        buf += rx(s, n)
        if not b1 & 0x80: continue
        msg = json.loads(buf); buf = b""
        if msg.get("id") == 1:
            s.close()
            return msg
cmd = sys.argv[1]
if cmd == "list":
    print(json.dumps([{k: t.get(k) for k in ("id", "type", "url", "title")} for t in targets()]))
elif cmd == "eval":
    r = call(find(sys.argv[2]), "Runtime.evaluate", {"expression": sys.argv[3], "awaitPromise": True, "returnByValue": True})
    res = r.get("result", {})
    print(json.dumps({"value": res.get("result", {}).get("value"), "error": res.get("exceptionDetails", {}).get("text")}))
elif cmd == "nav":
    print(json.dumps(call(find(sys.argv[2]), "Page.navigate", {"url": sys.argv[3]}).get("result")))
elif cmd == "new":
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/json/new?{sys.argv[2]}", method="PUT")
    print(json.dumps(json.load(urllib.request.urlopen(req, timeout=10))))
'''

VPNQ_PY = r'''
import json, socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(200)
s.connect("/run/neox-vpn/vpn.sock")
s.sendall((json.dumps({"method": sys.argv[1], "params": json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}}) + "\n").encode())
print(s.makefile("rb").readline().decode().strip())
'''


class Neox:
    def __init__(self, env: str):
        self.env = env
        self.page: str | None = None

    def cdp(self, *args: str, timeout: float = 120) -> dict:
        out = dc.as_user(self.env, "python3 /tmp/cdp.py " + " ".join(shlex.quote(a) for a in args), timeout)["out"]
        lines = [l for l in out.strip().splitlines() if l.startswith(("{", "["))]
        try:
            return json.loads(lines[-1]) if lines else {"error": out.strip()[-300:]}
        except ValueError:
            return {"error": out.strip()[-300:]}

    def panel(self, js: str, timeout: float = 120):
        r = self.cdp("eval", "sidepanel.html", js, timeout=timeout)
        if r.get("error"):
            print(f"  panel js error: {r['error']}", flush=True)
        return r.get("value")

    def ip(self) -> tuple[str | None, str]:
        """What ifconfig.co tells NEOX: (exit IP or None, what the page showed)."""
        self.cdp("nav", self.page, "https://ifconfig.co/json")
        time.sleep(10)
        text = self.cdp("eval", self.page, "document.body ? document.body.innerText : ''").get("value") or ""
        try:
            j = json.loads(text)
            return j.get("ip"), f"{j.get('ip')} {j.get('country_iso')} {j.get('city') or ''}".strip()
        except ValueError:
            return None, " ".join(text.split())[:120]


def system_ip(user_env: str | None = None) -> dict:
    cmd = "curl -sS -m 25 -H 'Accept: application/json' https://ifconfig.co/json"
    out = (dc.as_user(user_env, cmd) if user_env else sh(cmd))["out"].strip()
    try:
        return json.loads(out[out.index("{"):])
    except ValueError:
        return {"ip": None, "error": out[-160:]}


def vpnq(env: str, method: str, params: dict | None = None, timeout: float = 220) -> dict:
    out = dc.as_user(env, f"python3 /tmp/vpnq.py {method} {shlex.quote(json.dumps(params or {}))}", timeout)["out"]
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": out.strip()[-200:]}


def wait_status(env: str, pred, timeout: float) -> dict:
    end, st = time.monotonic() + timeout, {}
    while time.monotonic() < end:
        st = vpnq(env, "status")
        if pred(st):
            return st
        time.sleep(4)
    return st


CONNECT_JS = """(async () => {
  const $ = (id) => document.getElementById(id), sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  document.querySelector('#vpn-backend [data-b=%(backend)s]').click();
  await sleep(300);
  if (%(place)s && !$('vpn-place').querySelector('option[value=%(place)s]')) return 'no place option';
  $('vpn-place').value = %(place)s || '';
  const hop = $('vpn-hop'); hop.checked = %(hop)s; hop.dispatchEvent(new Event('change'));
  $('vpn-mins').value = '1';
  for (const i of document.querySelectorAll('#vpn-places input')) i.checked = %(places)s.includes(i.value);
  $('vpn-connect').click();
  await sleep(300);
  if ($('vpn-scope').hidden) return 'scope chooser not shown: ' + $('vpn-err').textContent;
  document.querySelector('#vpn-scope [data-s=%(scope)s]').click();
  return 'clicked';
})()"""


def connect_js(backend: str, scope: str, place: str = "", hop: bool = False, places=()) -> str:
    return CONNECT_JS % {"backend": backend, "scope": scope, "place": json.dumps(place), "hop": json.dumps(hop),
                         "places": json.dumps(list(places))}


PANEL_STATE = "JSON.stringify({title: document.getElementById('vpn-title').textContent, " \
              "pill: document.getElementById('vpn-pill').textContent, sub: document.getElementById('vpn-sub').textContent, " \
              "held: !document.getElementById('vpn-held').hidden, err: document.getElementById('vpn-err').textContent})"


def main() -> int:
    vm.stop(D)
    time.sleep(2)
    for f in ("desk.qcow2", "OVMF_VARS.fd"):
        (D / f).unlink(missing_ok=True)
    vm.start(D, p8.ISO, D / "desk.qcow2", "cd")
    print(f"live system booted; guest agent after {p8.wait_agent():.0f}s", flush=True)
    env = ""
    for _ in range(60):
        env = dc.session_env()
        if env:
            break
        time.sleep(5)
    dc.wait_window(env, r"^AIRA$", 180)
    time.sleep(15)
    r: dict[str, bool] = {}
    sh("cat > /tmp/cdp.py <<'PY'\n" + CDP_PY + "\nPY\ncat > /tmp/vpnq.py <<'PY'\n" + VPNQ_PY + "\nPY\nchmod 644 /tmp/cdp.py /tmp/vpnq.py")

    # ------------------------------------------------------------ the service
    pk = ["neox", "openvpn", "wireguard-tools", "network-manager-openvpn", "nftables", "tor", "tor-geoipdb"]
    st = sh("dpkg-query -W -f='${Package} ${Status}\\n' " + " ".join(pk))["out"]
    missing = [p for p in pk if f"{p} install ok installed" not in st]
    active = sh("systemctl is-active neox-vpnd")["out"].strip()
    first = vpnq(env, "status")
    print(f"packages missing: {missing}; neox-vpnd {active}; status as {USER}: {first}", flush=True)
    r["service"] = not missing and active == "active" and first.get("ok") is True and first.get("connected") is False

    home = system_ip()
    print(f"this VM's own exit: {home.get('ip')} {home.get('country_iso')}", flush=True)
    r["home_ip"] = bool(home.get("ip"))

    # ------------------------------------------------------------ NEOX with its VPN tab
    dc.as_user(env, f"setsid neox --remote-debugging-port={PORT} https://ifconfig.co/json >/dev/null 2>&1 &")
    win = dc.wait_window(env, r"(?i)ifconfig", 120)
    time.sleep(6)
    nx = Neox(env)
    listing = nx.cdp("list")
    pages = [t for t in listing if isinstance(t, dict) and t.get("type") == "page" and "ifconfig" in t.get("url", "")] \
        if isinstance(listing, list) else []
    nx.page = pages[0]["id"] if pages else "ifconfig.co"
    before, seen = nx.ip()
    print(f"NEOX window {win}; NEOX before the VPN: {seen}", flush=True)
    if win:
        vm.click(D, win[1] + win[3] // 2, win[2] + win[4] // 2)
    time.sleep(1)
    vm.key(D, "ctrl-shift-y")                     # the side panel (a key press: Chromium wants a user gesture)
    time.sleep(6)
    listing = nx.cdp("list")
    if not isinstance(listing, list) or not any("sidepanel.html" in t.get("url", "") for t in listing):
        print("  side panel did not open from the key; opening the panel page in a tab", flush=True)
        nx.cdp("new", f"chrome-extension://{EXT}/sidepanel.html")
        time.sleep(4)
    nx.panel("document.getElementById('tab-vpn').click(); 1")
    for _ in range(30):                           # Riseup's places come from its API through neox-vpnd
        if nx.panel("document.querySelectorAll('#vpn-place option[value=Paris]').length"):
            break
        time.sleep(2)

    # ------------------------------------------------------------ Riseup, only NEOX, hopping every minute
    print("connect:", nx.panel(connect_js("riseup", "neox", "Paris", True, ["Paris", "Amsterdam"]), 200), flush=True)
    st = wait_status(env, lambda s: s.get("connected") is True, 90)
    conn = st.get("connection") or {}
    time.sleep(3)
    ip1, seen1 = nx.ip()
    sys1 = system_ip()
    panel = json.loads(nx.panel(PANEL_STATE) or "{}")
    print(f"only NEOX: {conn.get('server')} {conn.get('city')}; NEOX sees {seen1}; system sees {sys1.get('ip')}; panel {panel}",
          flush=True)
    r["neox_only"] = bool(ip1) and ip1 != home.get("ip") and sys1.get("ip") == home.get("ip") \
        and conn.get("scope") == "neox" and "Paris" in panel.get("title", "")
    vm.shot(D, SHOTS / "neox-vpn.png")
    r["screenshot"] = (SHOTS / "neox-vpn.png").exists()

    st = wait_status(env, lambda s: (s.get("connection") or {}).get("place") == "Amsterdam"
                     and (s.get("connection") or {}).get("state") == "up", 150)
    conn2 = st.get("connection") or {}
    time.sleep(6)
    ip2, seen2 = nx.ip()
    panel = json.loads(nx.panel(PANEL_STATE) or "{}")
    print(f"hop: {conn2.get('server')} {conn2.get('city')} (error {st.get('error')}); NEOX sees {seen2}; panel {panel}",
          flush=True)
    r["hop"] = conn2.get("place") == "Amsterdam" and bool(ip2) and ip2 not in (ip1, home.get("ip")) \
        and "Amsterdam" in panel.get("title", "")

    # ------------------------------------------------------------ kill switch
    sh("systemctl stop neox-vpnd")
    time.sleep(14)                                 # NEOX checks the service every 10 seconds
    ip3, seen3 = nx.ip()
    panel = json.loads(nx.panel(PANEL_STATE) or "{}")
    sh("systemctl start neox-vpnd")
    time.sleep(4)
    nx.panel("document.getElementById('vpn-release').click(); 1")
    time.sleep(4)
    ip4, seen4 = nx.ip()
    print(f"kill switch: service stopped -> NEOX sees {seen3!r}, panel {panel}; turned off -> NEOX sees {seen4}", flush=True)
    r["kill_switch"] = ip3 is None and panel.get("held") is True and ip4 == home.get("ip")

    # ------------------------------------------------------------ Riseup, whole computer
    nx.panel("location.reload(); 1")
    time.sleep(5)
    nx.panel("document.getElementById('tab-vpn').click(); 1")
    for _ in range(30):
        if nx.panel("document.querySelectorAll('#vpn-place option[value=Paris]').length"):
            break
        time.sleep(2)
    print("connect:", nx.panel(connect_js("riseup", "system"), 200), flush=True)
    st = wait_status(env, lambda s: s.get("connected") is True or bool(s.get("error")), 120)
    sys5 = system_ip()
    nm = sh("nmcli -t -f NAME,TYPE,DEVICE connection show --active")["out"]
    print(f"whole computer (Riseup): {st.get('connection')} error {st.get('error')}; system sees {sys5}; NM: {nm.strip()}",
          flush=True)
    nx.panel("document.getElementById('vpn-disconnect').click(); 1")
    wait_status(env, lambda s: s.get("connection") is None, 60)
    time.sleep(4)
    sys6 = system_ip()
    print(f"  after disconnect: system sees {sys6.get('ip')}", flush=True)
    r["system_riseup"] = st.get("connected") is True and sys5.get("ip") not in (None, home.get("ip")) \
        and "NEOX VPN" in nm and sys6.get("ip") == home.get("ip")

    # ------------------------------------------------------------ Tor, whole computer
    print("connect:", nx.panel(connect_js("tor", "system", "de"), 200), flush=True)
    st = wait_status(env, lambda s: s.get("connected") is True or bool(s.get("error")), 180)
    sys7 = system_ip(env)
    dns = sh("getent hosts debian.org")["out"].strip()
    table = sh("nft list table inet neox_vpn >/dev/null 2>&1 && echo present || echo absent")["out"].strip()
    print(f"whole computer (Tor DE): {st.get('connection')} error {st.get('error')}; user sees {sys7}; dns {dns!r}; "
          f"table {table}", flush=True)
    nx.panel("document.getElementById('vpn-disconnect').click(); 1")
    wait_status(env, lambda s: s.get("connection") is None, 60)
    time.sleep(3)
    sys8 = system_ip(env)
    table2 = sh("nft list table inet neox_vpn >/dev/null 2>&1 && echo present || echo absent")["out"].strip()
    print(f"  after disconnect: user sees {sys8.get('ip')}; table {table2}", flush=True)
    r["system_tor"] = st.get("connected") is True and sys7.get("country_iso") == "DE" and bool(dns) \
        and table == "present" and sys8.get("ip") == home.get("ip") and table2 == "absent"

    print("\n=== NEOX VPN check:", "PASS" if all(r.values()) else "FAIL", r)
    return 0 if all(r.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
