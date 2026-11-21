"""AIROX Connect (PC side): security pieces, WebSocket framing, H.264 splitting, files, and the broker
gating phone actions with confirmations sent to the phone that asked."""
from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import time
from pathlib import Path

import pytest

from airox_connect import h264, ws
from airox_connect.files import FilesError, Transfers, list_dir, read_range, resolve
from airox_connect.security import Devices, Limiter, Pairing, lan_ok


# ------------------------------------------------------------------ security
@pytest.mark.parametrize("ip,ok", [("192.168.1.20", True), ("10.0.0.5", True), ("172.16.3.4", True),
                                   ("127.0.0.1", True), ("169.254.1.1", True), ("fe80::1", True), ("fd12::3", True),
                                   ("::ffff:192.168.1.7", True), ("8.8.8.8", False), ("100.64.1.2", False),
                                   ("2a00:1450::1", False), ("0.0.0.0", False), ("not-an-ip", False)])
def test_lan_only(ip, ok):
    assert lan_ok(ip) is ok


def test_pairing_code_single_use_and_expiry():
    p = Pairing(seconds=0.3)
    code, _ = p.new_code()
    assert len(code) == 32 and not p.claim("0" * 32)
    assert p.claim(code) and not p.claim(code)              # works once
    code, _ = p.new_code()
    time.sleep(0.4)
    assert not p.claim(code)                                 # expired


def test_pairing_needs_pc_answer():
    p = Pairing()
    r = p.request({"name": "Pixel"})
    threading.Timer(0.1, lambda: p.answer(r["id"], True)).start()
    assert p.wait(r, 2) is True
    r = p.request({"name": "Other"})
    assert p.wait(r, 0.2) is False                           # nobody answered: refused


def test_devices_store_only_token_hash(tmp_path):
    d = Devices(tmp_path / "devices.json")
    dev_id, token = d.add("Pixel 8", "Google Pixel 8")
    raw = (tmp_path / "devices.json").read_text()
    assert token not in raw and hashlib.sha256(token.encode()).hexdigest() in raw
    assert oct((tmp_path / "devices.json").stat().st_mode & 0o777) == "0o600"
    assert d.verify(f"Bearer {dev_id}.{token}")["name"] == "Pixel 8"
    assert d.verify(f"Bearer {dev_id}.{token}x") is None and d.verify(f"Bearer other.{token}") is None
    assert d.verify(token) is None
    assert d.revoke(dev_id) and d.verify(f"Bearer {dev_id}.{token}") is None


def test_limiter():
    lim = Limiter(n=3, window=10)
    for _ in range(3):
        assert not lim.blocked("1.2.3.4")
        lim.fail("1.2.3.4")
    assert lim.blocked("1.2.3.4") and not lim.blocked("1.2.3.5")


# ------------------------------------------------------------------ WebSocket framing
def client_frame(op: int, payload: bytes, fin: bool = True) -> bytes:
    f = ws.encode(op, payload, mask=b"\x01\x02\x03\x04")
    return f if fin else bytes([f[0] & 0x7F]) + f[1:]


def test_websocket_roundtrip_fragments_and_ping():
    big = os.urandom(70000)
    data = (client_frame(ws.PING, b"hi") + client_frame(ws.TEXT, b"hel", fin=False) + client_frame(ws.CONT, b"lo")
            + client_frame(ws.BINARY, big) + client_frame(ws.CLOSE, b"\x03\xe8"))
    out = io.BytesIO()
    w = ws.WebSocket(io.BytesIO(data), out)
    assert w.recv() == (ws.TEXT, b"hello")
    assert out.getvalue().startswith(bytes([0x80 | ws.PONG, 2]) + b"hi")
    assert w.recv() == (ws.BINARY, big)
    with pytest.raises(ws.Closed):
        w.recv()


def test_websocket_refuses_unmasked_frames():
    w = ws.WebSocket(io.BytesIO(ws.encode(ws.TEXT, b"x")), io.BytesIO())
    with pytest.raises(ws.Closed):
        w.recv()


def test_accept_key_rfc_example():
    assert ws.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


# ------------------------------------------------------------------ H.264 access units
def nal(t: int, first: bool = True, size: int = 20) -> bytes:
    return b"\x00\x00\x00\x01" + bytes([0x60 | t, 0x80 if first else 0x00]) + os.urandom(size).replace(b"\x00", b"\x01")


def test_splitter_keeps_slices_of_one_frame_together():
    frame1 = nal(7) + nal(8) + nal(5, True) + nal(5, False)          # SPS, PPS, IDR in two slices
    frame2 = nal(1, True) + nal(1, False) + nal(1, False)            # P frame, three slices
    p3 = nal(1, True)
    frame3 = nal(9) + p3                                             # delimiter, P frame
    s = h264.Splitter()
    stream = frame1 + frame2 + frame3
    aus = []
    for i in range(0, len(stream), 7):                               # arbitrary chunking
        aus += s.feed(stream[i:i + 7])
    aus.append(s.flush())
    assert aus == [frame1, frame2, p3]                               # the delimiter is dropped
    assert h264.is_key(aus[0]) and not h264.is_key(aus[1])


# ------------------------------------------------------------------ files
@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / "Pictures").mkdir(parents=True)
    (h / "Pictures" / "cat.jpg").write_bytes(b"\xff\xd8cat")
    (h / ".ssh").mkdir()
    (h / ".ssh" / "id_ed25519").write_text("secret")
    (tmp_path / "outside.txt").write_text("no")
    (h / "link").symlink_to(tmp_path / "outside.txt")
    return h


def test_resolve_stays_in_home(home):
    assert resolve("~/Pictures/cat.jpg", root=home) == home / "Pictures" / "cat.jpg"
    for bad in ("~/../outside.txt", "/etc/passwd", "~/link", "~/.ssh/id_ed25519"):
        with pytest.raises(FilesError):
            resolve(bad, root=home)
    assert resolve("~/.ssh/id_ed25519", show_hidden=True, root=home).name == "id_ed25519"


def test_list_dir(home):
    out = list_dir("~", root=home)
    names = [i["name"] for i in out["items"]]
    assert "Pictures" in names and ".ssh" not in names and out["parent"] is None
    pics = list_dir("~/Pictures", root=home)
    assert pics["items"][0]["kind"] == "image" and pics["parent"] == "~"


def test_upload_resume_check_and_no_overwrite(tmp_path, home):
    inbox = tmp_path / "inbox"
    t = Transfers(inbox, root=home)
    data = os.urandom(300000)
    sha = hashlib.sha256(data).hexdigest()
    r = t.upload_chunk("dev1", "upload-0001", "photo.jpg", len(data), sha, 0, iter([data[:100000]]))
    assert r == {"received": 100000, "complete": False} and t.upload_status("dev1", "upload-0001") == 100000
    with pytest.raises(FilesError, match="resume at byte 100000"):
        t.upload_chunk("dev1", "upload-0001", "photo.jpg", len(data), sha, 0, iter([data]))
    r = t.upload_chunk("dev1", "upload-0001", "photo.jpg", len(data), sha, 100000, iter([data[100000:]]))
    assert r["complete"] and Path(r["saved"]).read_bytes() == data and r["name"] == "photo.jpg"
    r2 = t.upload_chunk("dev1", "upload-0002", "photo.jpg", 3, hashlib.sha256(b"abc").hexdigest(), 0, iter([b"abc"]))
    assert r2["name"] == "photo (1).jpg" and (inbox / "photo.jpg").read_bytes() == data     # never overwritten
    with pytest.raises(FilesError, match="damaged"):
        t.upload_chunk("dev1", "upload-0003", "x.bin", 3, "0" * 64, 0, iter([b"abc"]))
    for bad in ("../x", "a/b", ""):
        with pytest.raises(FilesError):
            t.upload_chunk("dev1", "upload-0004", bad, 1, sha, 0, iter([b"a"]))


def test_offers_are_per_device(home, tmp_path):
    t = Transfers(tmp_path / "inbox", root=home)
    [o] = t.offer("dev1", ["~/Pictures/cat.jpg"])
    assert t.get_offer("dev1", o["tid"])["size"] == 5
    with pytest.raises(FilesError):
        t.get_offer("dev2", o["tid"])
    assert t.offer_hash("dev1", o["tid"]) == hashlib.sha256(b"\xff\xd8cat").hexdigest()
    with pytest.raises(FilesError):
        t.offer("dev1", ["~/link"])


def test_read_range():
    assert read_range(Path("x"), None, 10) == (0, 9)
    assert read_range(Path("x"), "bytes=4-", 10) == (4, 9)
    assert read_range(Path("x"), "bytes=-3", 10) == (7, 9)
    with pytest.raises(FilesError):
        read_range(Path("x"), "bytes=10-", 10)


# ------------------------------------------------------------------ the service: broker and confirmations
class FakeWS:
    def __init__(self, answer=None):
        self.sent, self.answer, self.svc = [], answer, None

    def send_text(self, text):
        m = json.loads(text)
        self.sent.append(m)
        if m.get("t") == "confirm" and self.answer is not None:
            threading.Timer(0.05, lambda: self.svc.confirms.answer(m["id"], self.answer)).start()

    def close(self, code=1000):
        pass


@pytest.fixture
def svc(tmp_path, monkeypatch, home):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    (tmp_path / "run").mkdir()
    monkeypatch.setenv("HOME", str(home))
    from airox_connect.server import Service
    s = Service(port=0, inbox=tmp_path / "inbox", log_path=tmp_path / "actions.jsonl", start_desktop=False)
    s.transfers.root = home
    yield s
    s.stop_lan()


def connect(svc, answer):
    from airox_connect.server import Conn
    dev_id, _ = svc.devices.add("Pixel")
    dev = svc.devices.get(dev_id)
    fw = FakeWS(answer)
    fw.svc = svc
    svc.hub.add(Conn(fw, dev, "192.168.1.9"))
    return dev, fw


def test_send_files_asks_the_phone_and_logs(svc, tmp_path):
    dev, fw = connect(svc, answer=False)
    out = svc.run("connect", "send_files", {"paths": ["~/Pictures/cat.jpg"], "device": dev["id"]}, dev)
    assert not out["allowed"] and out["risks"] == ["send_off_machine"]
    ask = next(m for m in fw.sent if m["t"] == "confirm")
    assert ask["device"] == dev["id"] and "cat.jpg" in ask["summary"] and ask["strong"] is False
    fw.answer = True
    out = svc.run("connect", "send_files", {"paths": ["~/Pictures/cat.jpg"], "device": dev["id"]}, dev)
    assert out["ok"] and out["data"]["offers"][0]["name"] == "cat.jpg"
    log = [json.loads(l) for l in (tmp_path / "actions.jsonl").read_text().splitlines()]
    assert [e["allowed"] for e in log if e["tool"] == "send_files"] == [False, True]
    assert all(e["via"] == "AIROX Connect (Pixel)" for e in log)


def test_offline_phone_is_never_asked_so_denied(svc):
    dev_id, _ = svc.devices.add("Away")
    out = svc.run("connect", "send_files", {"paths": ["~/Pictures/cat.jpg"], "device": dev_id}, svc.devices.get(dev_id))
    assert not out["allowed"]


def test_read_only_and_writes_run_without_asking(svc):
    dev, fw = connect(svc, answer=False)
    out = svc.run("connect", "receive_file", {"name": "a.jpg", "size": "3"}, dev)
    assert out["ok"] and not any(m["t"] == "confirm" for m in fw.sent)
    out = svc.run("connect", "power", {"action": "shutdown"}, dev)
    assert not out["allowed"] and out["risks"] == ["stop"]


def test_task_manager_actions_from_the_phone(svc):
    import subprocess
    import sys
    dev, fw = connect(svc, answer=True)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        svc.tm.tick()
        p = svc.tm.find(child.pid)
        out = svc.run("taskmgr", "end_task", {"pid": child.pid, "start": p.start}, dev)
        assert out["ok"] and out["data"]["verified"]
        assert any(m["t"] == "confirm" and m["source"] == "taskmgr" for m in fw.sent)
        assert child.wait(5) is not None
    finally:
        if child.poll() is None:
            child.kill()


def test_unknown_tool_and_bad_args_are_rejected(svc):
    dev, _ = connect(svc, answer=True)
    assert not svc.run("connect", "format_disk", {}, dev)["allowed"]
    assert not svc.run("connect", "media", {"action": "explode"}, dev)["allowed"]
