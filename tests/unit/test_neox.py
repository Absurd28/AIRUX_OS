"""NEOX bridge: AIRA's commands reach NEOX's extension through the native host and come back."""
from __future__ import annotations

import http.server
import io
import json
import os
import threading
import time

import pytest

from aira import neox, neox_host, neox_qa
from aira.browser import BrowserError

PAGE = {"url": "https://example.org/plan", "title": "Pro plan",
        "text": "The Pro plan costs $40 per month and includes 2 TB of storage. Cancel any time."}


def test_native_framing_round_trip():
    buf = io.BytesIO()
    neox.write_native(buf, {"type": "result", "id": "a1", "value": {"url": "https://x.org"}})
    buf.seek(0)
    assert neox.read_native(buf) == {"type": "result", "id": "a1", "value": {"url": "https://x.org"}}
    assert neox.read_native(buf) is None          # closed stream


def test_page_answers_must_quote_the_page():
    ok = neox_qa.check('It costs "$40 per month" and has "2 TB of storage".', PAGE["text"])
    assert ok["status"] == "verified" and not ok["unverified"]
    made_up = neox_qa.check('It costs "$40 per month" and "includes free phone support".', PAGE["text"])
    assert made_up["status"] == "unverified" and made_up["unverified"] == ["includes free phone support"]
    assert neox_qa.check("I'm not sure, this page doesn't say.", PAGE["text"])["status"] == "not sure"
    assert neox_qa.check("It is a good plan.", PAGE["text"])["status"] == "unverified"
    # curly quotes and spacing differences still match
    assert neox_qa.check("“Cancel  any time”", PAGE["text"])["status"] == "verified"
    msgs = neox_qa.messages("How much?", PAGE)
    assert "ONLY" in msgs[0]["content"] and "$40 per month" in msgs[0]["content"]


class FakeExtension(threading.Thread):
    """Plays NEOX's extension on the other side of the native host's stdin/stdout."""

    def __init__(self, from_host, to_host):
        super().__init__(daemon=True)
        self.from_host, self.to_host, self.seen = from_host, to_host, []

    def run(self):
        while True:
            msg = neox.read_native(self.from_host)
            if msg is None:
                return
            self.seen.append(msg)
            if msg.get("type") != "command":
                continue
            if msg["cmd"] == "click" and msg.get("text") == "missing":
                out = {"type": "result", "id": msg["id"], "ok": False, "error": 'no link or button with the text "missing"'}
            else:
                url = "https://duckduckgo.com/?q=" + msg.get("query", "").replace(" ", "+") if msg["cmd"] == "search" \
                    else msg.get("url") or "https://example.org/"
                out = {"type": "result", "id": msg["id"], "ok": True,
                       "value": {"url": url, "title": "t", "text": "results", "outbound_links": 5, "links": []}}
            neox.write_native(self.to_host, out)


@pytest.fixture
def wired(tmp_path):
    """AIRA's bridge <-> native host <-> fake NEOX extension, all in-process over real pipes and a socket."""
    sock = tmp_path / "run" / "neox.sock"
    bridge = neox.NeoxBridge(sock_path=sock, launch=[], wait_s=5, timeout_s=10)
    h_in_r, h_in_w = os.pipe()      # extension -> host stdin
    h_out_r, h_out_w = os.pipe()    # host stdout -> extension
    host_stdin, ext_out = os.fdopen(h_in_r, "rb"), os.fdopen(h_in_w, "wb")
    host_stdout, ext_in = os.fdopen(h_out_w, "wb"), os.fdopen(h_out_r, "rb")
    host = neox_host.Host(host_stdin, host_stdout, sock_path=sock)
    threading.Thread(target=host.run, daemon=True).start()
    ext = FakeExtension(ext_in, ext_out)
    ext.start()
    for _ in range(50):
        if bridge.connected:
            break
        time.sleep(0.1)
    yield bridge, ext
    host.running = False
    bridge.close()
    ext_out.close()


def test_aira_commands_run_in_neox_and_come_back(wired):
    bridge, ext = wired
    assert bridge.connected
    st = bridge.search("debian trixie")
    assert st["url"] == "https://duckduckgo.com/?q=debian+trixie" and st["outbound_links"] == 5
    assert bridge.open("example.org")["url"] == "example.org"
    assert bridge.read()["title"] == "t"
    with pytest.raises(BrowserError, match="no link or button"):
        bridge.click("missing")
    assert [m["cmd"] for m in ext.seen if m["type"] == "command"] == ["search", "open", "read", "click"]


def test_no_neox_is_reported_plainly(tmp_path):
    b = neox.NeoxBridge(sock_path=tmp_path / "s" / "neox.sock", launch=[], wait_s=1)
    with pytest.raises(BrowserError, match="not installed"):
        b.read()
    b.close()


def test_socket_is_private_and_not_stolen(tmp_path):
    sock = tmp_path / "r" / "neox.sock"
    b = neox.NeoxBridge(sock_path=sock, launch=[])
    assert oct(sock.stat().st_mode & 0o777) == "0o600" and oct(sock.parent.stat().st_mode & 0o777) == "0o700"
    with pytest.raises(BrowserError, match="already connected"):
        neox.NeoxBridge(sock_path=sock, launch=[])
    b.close()


def test_side_panel_question_is_answered_from_the_page(tmp_path):
    answers = ['The Pro plan costs "$40 a month".', 'The Pro plan costs "$40 per month".']   # misquote, then exact

    class Model(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert "$40 per month" in body["messages"][0]["content"]
            if len(body["messages"]) > 2:   # the retry names the quote that is not on the page
                assert '"$40 a month"' in body["messages"][-1]["content"]
            out = {"choices": [{"message": {"content": answers.pop(0)}}]}
            data = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    out = io.BytesIO()
    h = neox_host.Host(io.BytesIO(), out, sock_path=tmp_path / "x.sock", model_url=f"http://127.0.0.1:{srv.server_port}")
    h.ask_page({"type": "ask_page", "id": "p1", "question": "How much is Pro?", "page": PAGE})
    out.seek(0)
    msg = neox.read_native(out)
    assert msg["type"] == "answer" and msg["id"] == "p1" and msg["status"] == "verified" and not answers
    srv.shutdown()


def test_orchestrator_uses_neox_only_when_asked(tmp_path, monkeypatch):
    from aira.browser import BrowserSession
    from aira.config import Config
    from aira.orchestrator import make_browser
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    cfg = Config(data_dir=tmp_path / "data")
    monkeypatch.delenv("AIRA_BROWSER", raising=False)
    assert isinstance(make_browser(cfg), BrowserSession)
    monkeypatch.setenv("AIRA_BROWSER", "neox")
    first = make_browser(cfg)
    assert isinstance(first, neox.NeoxBridge)
    assert isinstance(make_browser(cfg), BrowserSession)   # a second AIRA process falls back
    first.close()
