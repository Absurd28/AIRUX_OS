"""AIRA shell and browser tests (scripted model; headless Chromium for the UI and the browser)."""
from __future__ import annotations

import http.server
import json
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from aira.shell import server as shell_server
from aira.shell.server import Shell, tags_for
from test_guard import call, make, reply, route


def post(port, path, body, token, host=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-AIRA-Token": token,
                                          **({"Host": host} if host else {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}


def wait_for(q: queue.Queue, event: str, timeout: float = 20) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            ev = q.get(timeout=0.5)
        except queue.Empty:
            continue
        if ev.get("event") == event:
            return ev
    raise AssertionError(f"no {event} event")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "notes").mkdir(parents=True)
    (h / "report.txt").write_text("draft\n")
    monkeypatch.setenv("HOME", str(h))
    return h


@pytest.fixture
def running(home, tmp_path):
    shells = []

    def start(script, confirm_timeout=None):
        sh = Shell(port=0)
        if confirm_timeout is not None:
            shell_server.CONFIRM_TIMEOUT = confirm_timeout
        orch, llm, executed = make(home, tmp_path, script, confirm=sh.confirm)
        sh.attach(orch)
        sh.serve()
        shells.append(sh)
        return sh, orch, executed
    yield start
    shell_server.CONFIRM_TIMEOUT = 120.0
    for s in shells:
        s.stop()


def test_tags():
    assert tags_for("Created ~/a.txt.\nVerified: ~/a.txt contains the text.") == ["verified"]
    assert set(tags_for("A [unsupported]\nSources: [1] x")) == {"unverified", "cited"}
    assert tags_for("Not done: delete_path needs your confirmation and it was not given.") == ["not done"]
    assert tags_for("I'm not sure. I don't have a tool that can check that.") == ["not sure"]


def test_requests_need_token_and_local_host(running):
    sh, _, executed = running([])
    assert post(sh.port, "/command", {"text": "hi"}, "wrong")[0] == 403
    assert post(sh.port, "/command", {"text": "hi"}, sh.token, host="evil.example:80")[0] == 403
    req = urllib.request.Request(f"http://127.0.0.1:{sh.port}/command", method="OPTIONS")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 403
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"http://127.0.0.1:{sh.port}/events?token=nope", timeout=5)
    page = urllib.request.urlopen(f"http://127.0.0.1:{sh.port}/", timeout=5).read().decode()
    assert sh.token in page and "Adaptive Intelligent Research Assistant" in page
    assert executed == []


def test_typed_command_flows_to_transcript(running):
    sh, _, _ = running([route("desktop", "list"), call("list_dir", path="~/notes"), reply("Your notes folder is empty.")])
    q = sh.subscribe()
    assert post(sh.port, "/command", {"text": "What's in my notes?"}, sh.token)[0] == 202
    assert wait_for(q, "you")["text"] == "What's in my notes?"
    assert wait_for(q, "agent_start")["agent"] == "desktop"
    ev = wait_for(q, "aira")
    assert ev["text"] == "Your notes folder is empty." and ev["calls"][0]["tool"] == "list_dir"


def test_panel_serves_before_aira_is_loaded(home, tmp_path):
    """The panel opens first; a command typed while AIRA loads waits for her, then runs."""
    sh = Shell(port=0)
    sh.serve()
    try:
        q = sh.subscribe()
        page = urllib.request.urlopen(f"http://127.0.0.1:{sh.port}/", timeout=5).read().decode()
        assert sh.token in page
        assert post(sh.port, "/settings", {}, sh.token)[0] == 503          # "AIRA is not ready"
        assert post(sh.port, "/command", {"text": "What's in my notes?"}, sh.token)[0] == 202
        wait_for(q, "thinking")
        time.sleep(0.5)
        orch, _, _ = make(home, tmp_path, [route("desktop", "list"), call("list_dir", path="~/notes"),
                                           reply("Your notes folder is empty.")])
        sh.attach(orch)
        assert wait_for(q, "aira")["text"] == "Your notes folder is empty."
    finally:
        sh.stop()


def test_command_gives_up_if_aira_never_loads(monkeypatch):
    monkeypatch.setattr(shell_server, "READY_WAIT", 0.3)
    sh = Shell(port=0)
    sh.serve()
    try:
        q = sh.subscribe()
        post(sh.port, "/command", {"text": "hi"}, sh.token)
        assert wait_for(q, "aira")["text"] == shell_server.STARTING
    finally:
        sh.stop()


def test_confirmation_is_asked_in_the_shell_and_denied(running, home):
    script = [route("desktop", "delete report"), call("delete_path", path="~/report.txt"),
              call("delete_path", path="~/report.txt"), call("delete_path", path="~/report.txt"),
              reply("Deleted.")]
    sh, _, executed = running(script)
    q = sh.subscribe()
    post(sh.port, "/command", {"text": "Delete ~/report.txt"}, sh.token)
    ev = wait_for(q, "confirm")
    assert ev["tool"] == "delete_path" and ev["risks"] == ["delete"]
    assert post(sh.port, "/confirm", {"id": ev["id"], "yes": False}, sh.token)[0] == 200
    assert wait_for(q, "confirm_done")["yes"] is False
    out = wait_for(q, "aira")
    assert "Not done" in out["text"] and "not done" in out["tags"]
    assert executed == [] and (home / "report.txt").exists()


def test_unanswered_confirmation_is_denied(running, home):
    script = [route("desktop", "delete report"), call("delete_path", path="~/report.txt"),
              call("delete_path", path="~/report.txt"), call("delete_path", path="~/report.txt"), reply("x")]
    sh, _, executed = running(script, confirm_timeout=1.0)
    q = sh.subscribe()
    post(sh.port, "/command", {"text": "Delete ~/report.txt"}, sh.token)
    assert "denied" in wait_for(q, "confirm_done")["why"]
    assert executed == [] and (home / "report.txt").exists()


def test_shell_ui_in_chromium(running, home):
    from playwright.sync_api import sync_playwright
    script = [route("desktop", "delete report"), call("delete_path", path="~/report.txt"),
              call("delete_path", path="~/report.txt"), call("delete_path", path="~/report.txt"), reply("x")]
    sh, _, executed = running(script)
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path="/usr/bin/chromium", headless=True)
        pg = b.new_page()
        pg.goto(f"http://127.0.0.1:{sh.port}/")
        assert pg.inner_text("header") .startswith("AIRA")
        pg.fill("#text", "Delete ~/report.txt")
        pg.press("#text", "Enter")
        pg.wait_for_selector(".confirm", timeout=20000)
        assert "delete_path" in pg.inner_text(".confirm") and "Risk: delete" in pg.inner_text(".confirm")
        pg.click(".confirm .no")
        pg.wait_for_selector(".msg.aira .tag.not-done", timeout=20000)
        assert pg.query_selector(".confirm") is None
        assert "Delete ~/report.txt" in pg.inner_text(".msg.you")
        b.close()
    assert executed == [] and (home / "report.txt").exists()


# ---------- browser tool ----------

@pytest.fixture
def search_site():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query).get("q", [""])[0]
            if u.path == "/search":
                body = (f"<html><head><title>{q} - results</title></head><body><h1>Results for {q}</h1>"
                        + "".join(f"<p><a href='http://localhost:{self.server.server_port}/r{i}'>Result {i} about "
                                  f"{q}</a> some text to make the page long enough.</p>" for i in range(5))
                        + "</body></html>")
            else:
                body = f"<html><head><title>Page {u.path}</title></head><body><p>This is {u.path}.</p></body></html>"
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_port
    srv.shutdown()


def test_browser_tool_search_readback_click_back(tmp_path, search_site, monkeypatch):
    from aira import browser as browser_mod
    from aira.tools.desktop import make_browser_tool
    monkeypatch.setattr(browser_mod, "SEARCH_URL", f"http://127.0.0.1:{search_site}/search?q={{q}}")
    sess = browser_mod.BrowserSession(tmp_path / "profile", headless=True)
    fn, risk, rb = make_browser_tool(sess)
    try:
        assert risk({"action": "search", "query": "x"}) == {browser_mod_risk("SEND_OFF")}
        r = fn("search", query="sparse attention")
        assert "Results for sparse attention" in r["text"] and len(r["links"]) == 5
        ok, why = rb({"action": "search", "query": "sparse attention"}, r)
        assert ok, why
        assert r["outbound_links"] == 5
        r = fn("click", text="Result 2 about")
        assert r["url"].endswith("/r2") and "This is /r2." in r["text"]
        r = fn("back")
        assert "/search" in r["url"]
        assert fn("read")["title"] == "sparse attention - results"
    finally:
        sess.close()


def browser_mod_risk(name):
    from aira.tools.base import Risk
    return Risk[name]


def test_query_in_url():
    from aira.browser import query_in_url
    assert query_in_url("https://duckduckgo.com/?q=sparse+attention&ia=web", "Sparse attention")
    assert not query_in_url("https://duckduckgo.com/", "sparse attention")


def test_settings_page_reads_and_changes_with_readback(running, home, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("AIRA_USER_NAMES", raising=False)
    monkeypatch.delenv("AIRA_USER_NAME", raising=False)
    sh, orch, executed = running([])
    page = urllib.request.urlopen(f"http://127.0.0.1:{sh.port}/settings", timeout=5).read().decode()
    assert "<title>AIRA Settings</title>" in page and sh.token in page
    assert post(sh.port, "/settings", {"action": "get"}, "wrong")[0] == 403
    code, s = post(sh.port, "/settings", {"action": "get"}, sh.token)
    assert code == 200 and "Bella" in s["voice"]["choices"] and s["memory"]["min_mb"] == 64

    code, r = post(sh.port, "/settings", {"action": "set", "what": "voice", "value": "Bella"}, sh.token)
    assert code == 200 and r["ok"] and orch.vault.setting("voice") == "af_bella"
    assert post(sh.port, "/settings", {"action": "set", "what": "voice", "value": "robot"}, sh.token)[0] == 400

    code, r = post(sh.port, "/settings", {"action": "set", "what": "names", "value": ["Aryan", "boss"]}, sh.token)
    assert code == 200 and r["ok"] and orch.persona.names == ["Aryan", "boss"]
    assert (home / ".config" / "aira" / "aira.toml").read_text().strip() == 'names = ["Aryan", "boss"]'
    assert post(sh.port, "/settings", {"action": "set", "what": "names", "value": ["<script>"]}, sh.token)[0] == 400

    code, r = post(sh.port, "/settings", {"action": "set", "what": "memory_paused", "value": True}, sh.token)
    assert r["ok"] and orch.vault.paused
    post(sh.port, "/settings", {"action": "set", "what": "memory_paused", "value": False}, sh.token)
    assert not orch.vault.paused

    assert post(sh.port, "/settings", {"action": "set", "what": "quota_mb", "value": 32}, sh.token)[0] == 400
    code, r = post(sh.port, "/settings", {"action": "set", "what": "quota_mb", "value": 128}, sh.token)
    assert r["ok"] and orch.vault.quota == 128 * 1024 * 1024

    code, r = post(sh.port, "/settings", {"action": "set", "what": "export", "value": True}, sh.token)
    assert r["ok"] and Path(r["file"]).is_file() and Path(r["file"]).parent == home
    assert post(sh.port, "/settings", {"action": "set", "what": "root_shell"}, sh.token)[0] == 400
    assert executed == []   # settings never run AIRA's tools


def test_open_app_launches_with_kde_when_gtk_launch_is_missing(monkeypatch, tmp_path):
    from aira.tools import desktop
    entry = tmp_path / "org.kde.dolphin.desktop"
    entry.write_text("[Desktop Entry]\nName=Dolphin\nExec=dolphin\n")
    have = {"kioclient6"}
    monkeypatch.setattr(desktop.shutil, "which", lambda n: f"/usr/bin/{n}" if n in have else None)
    assert desktop._launch_command(entry, "/tmp") == ["kioclient6", "exec", str(entry), "/tmp"]
    have = {"gtk-launch", "kioclient6"}
    assert desktop._launch_command(entry)[0] == "gtk-launch"
    have = {"gio"}
    assert desktop._launch_command(entry)[:2] == ["gio", "launch"]
    have = set()
    with pytest.raises(desktop.ToolError):
        desktop._launch_command(entry)
