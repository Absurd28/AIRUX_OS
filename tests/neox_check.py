#!/usr/bin/env python3
"""NEOX check (WSL, virtual display): the NEOX extension, Shields and AIRA's bridge, end to end.

A Chromium with NEOX's extension and uBlock Origin Lite (from the staged neox package) is started by
Playwright; its NEOX extension connects through the real native host (aira.neox_host) to a real
NeoxBridge, like AIRA's. AIRA's real model answers the side panel. Pass requires:
  bridge      AIRA opens, reads, clicks, goes back and searches in NEOX; a missing link is an error
  shields     a tracker script is blocked or neutered, a library from a CDN loads
  gpc         Sec-GPC: 1 is sent and navigator.globalPrivacyControl is true
  farbling    the same canvas drawing hashes differently on two sites, identically on one
  side_panel  the side panel answers a question about the page with quotes found on the page
System-wide policies (HTTPS-only, cookies, search) are checked in QEMU (tests/desktop_check.py).
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))
WORK = Path(os.environ.get("AIRUX_WORK", "/srv/airux-work"))
NEOX = WORK / "build/deb/neox/usr/share/neox"
EXT_ID = "caeflmcomdjlfjeinfhpookmojmhiphd"

CANVAS = """() => { const c = document.createElement("canvas"); c.width = 200; c.height = 60;
  const x = c.getContext("2d"); x.textBaseline = "top"; x.font = "16px Arial"; x.fillStyle = "#f60";
  x.fillRect(10, 10, 100, 30); x.fillStyle = "#069"; x.fillText("NEOX fingerprint 123", 4, 20);
  return c.toDataURL(); }"""
SCRIPTS = """async (urls) => { const load = (u) => new Promise((res) => { const s = document.createElement("script");
  s.src = u; s.onload = () => res("onload"); s.onerror = () => res("blocked"); document.head.appendChild(s); });
  const out = {}; for (const u of urls) { const ev = await load(u); out[u] = [ev, performance.getEntriesByName(u).length]; }
  return out; }"""


def main() -> int:
    from playwright.sync_api import sync_playwright

    from aira import neox
    from fixture import start_server
    tmp = Path(tempfile.mkdtemp(prefix="neox-check-"))
    os.environ["XDG_RUNTIME_DIR"] = str(tmp / "run")
    (tmp / "run").mkdir()
    server, note = (None, os.environ["AIRA_SERVER"]) if "AIRA_SERVER" in os.environ \
        else start_server(8080, "neox-check-server.log")
    # the native host, as installed: Chromium starts it from the profile's NativeMessagingHosts
    host = tmp / "neox-host"
    host.write_text(f"#!/bin/sh\ncd {shlex.quote(str(REPO))}\nexec {shlex.quote(sys.executable)} -m aira.neox_host\n")
    host.chmod(0o755)
    profile = tmp / "profile"
    (profile / "NativeMessagingHosts").mkdir(parents=True)
    manifest = json.loads((REPO / "neox/native/org.airux.neox.json").read_text())
    manifest["path"] = str(host)
    (profile / "NativeMessagingHosts" / "org.airux.neox.json").write_text(json.dumps(manifest))

    r = {}
    bridge = neox.NeoxBridge(launch=[], wait_s=40, timeout_s=60)
    # Chromium is started like NEOX starts it; the test (only) attaches over a local debugging port.
    # (Playwright's own launcher hangs when an extension replaces the new tab page.)
    # like /usr/lib/neox/user-extensions: Chromium needs writable copies (it writes the compiled rules)
    for e in ("extension", "ubol"):
        shutil.copytree(NEOX / e, tmp / "exts" / e)
    exts = f"{tmp / 'exts' / 'extension'},{tmp / 'exts' / 'ubol'}"
    browser_proc = subprocess.Popen(
        ["/usr/lib/chromium/chromium", f"--user-data-dir={profile}", f"--load-extension={exts}", "--no-sandbox",
         "--no-first-run", "--no-default-browser-check", "--remote-debugging-port=9333", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=open(tmp / "chromium.log", "w"))
    try:
        with sync_playwright() as p:
            for _ in range(60):
                try:
                    cdp = p.chromium.connect_over_cdp("http://127.0.0.1:9333")
                    break
                except Exception:
                    time.sleep(0.5)
            ctx = cdp.contexts[0]
            for _ in range(80):
                if bridge.connected:
                    break
                time.sleep(0.5)
            print(f"NEOX extension connected to AIRA's bridge: {bridge.connected}", flush=True)

            # ---- AIRA browses in NEOX
            steps = {}
            try:
                st = bridge.open("https://example.com/")
                steps["open"] = st["url"].startswith("https://example.com") and "Example Domain" in st["title"]
                steps["read"] = "illustrative" in bridge.read()["text"] or "documentation" in bridge.read()["text"]
                link = (bridge.read().get("links") or [{}])[0]   # whatever link example.com has today
                st = bridge.click(link.get("text", "?"))
                steps["click"] = bool(link) and "example.com" not in st["url"]
                print(f"  clicked '{link.get('text')}' -> {st['url']}")
                steps["back"] = "example.com" in bridge.back()["url"]
                st = bridge.search("debian trixie release notes")
                steps["search"] = "duckduckgo.com" in st["url"] and "trixie" in st["url"]
                print(f"  search: {st['url']} ({st['outbound_links']} outbound links; DuckDuckGo may bot-check)")
                try:
                    bridge.click("a link that does not exist")
                    steps["missing_is_error"] = False
                except Exception as exc:
                    steps["missing_is_error"] = "no link or button" in str(exc)
            except Exception as exc:
                print("  bridge error:", exc)
            print(f"bridge steps: {steps}")
            r["bridge"] = len(steps) == 6 and all(steps.values())

            # ---- Shields, GPC, farbling (a Playwright page in the same browser)
            pg = ctx.new_page()
            pg.goto("https://example.com/", timeout=30000)
            time.sleep(2)
            res = pg.evaluate(SCRIPTS, ["https://connect.facebook.net/en_US/fbevents.js",
                                        "https://www.googletagmanager.com/gtag/js?id=G-TEST",
                                        "https://cdnjs.cloudflare.com/ajax/libs/jquery/3.7.1/jquery.min.js"])
            print(f"scripts: {res}")
            fb, gtm, jq = res.values()
            r["shields"] = fb[0] == "blocked" and gtm[1] == 0 and jq == ["onload", 1]
            gpc_flag = pg.evaluate("navigator.globalPrivacyControl")
            pg.goto("https://httpbin.org/headers", timeout=30000)
            hdr = pg.inner_text("body")
            r["gpc"] = gpc_flag is True and '"Sec-Gpc": "1"' in hdr
            print(f"GPC: navigator={gpc_flag}, header sent={'Sec-Gpc' in hdr}")
            pg.goto("https://example.com/", timeout=30000)
            a1, a2 = pg.evaluate(CANVAS), pg.evaluate(CANVAS)
            pg.goto("https://example.org/", timeout=30000)
            b1 = pg.evaluate(CANVAS)
            r["farbling"] = a1 == a2 and a1 != b1
            print(f"canvas: same site same={a1 == a2}, other site differs={a1 != b1}")

            # ---- the side panel (its page, as the user sees it) answers about the page
            pg.goto("https://example.com/", timeout=30000)
            pg.bring_to_front()
            sw = next(w for w in ctx.service_workers if EXT_ID in w.url)
            # extension pages may only be opened from the extension itself
            with ctx.expect_page() as info:
                sw.evaluate("chrome.tabs.create({url: 'sidepanel.html', active: false})")
            panel = info.value
            panel.wait_for_load_state()
            panel.fill("#page-q", "What is this domain for? Quote the page.")
            panel.eval_on_selector("#page-form", "f => f.requestSubmit()")
            panel.wait_for_selector("#page-log .tag, #page-log .msg.error", timeout=180000)
            ans = panel.inner_text("#page-log")
            print("side panel answer:", " ".join(ans.split())[:400])
            r["side_panel"] = "quotes found on the page" in ans
            panel.screenshot(path=str(REPO / "docs/screenshots/neox-sidepanel.png"))
            with ctx.expect_page() as info:
                sw.evaluate("chrome.tabs.create({})")     # a new tab: NEOX's page replaces Chromium's
            nt = info.value
            nt.wait_for_load_state()
            time.sleep(2)
            print("new tab page:", nt.url, "|", " ".join(nt.inner_text("body").split())[:160])
            nt.screenshot(path=str(REPO / "docs/screenshots/neox-newtab.png"))
            cdp.close()
    finally:
        browser_proc.terminate()
        bridge.close()
        if server is not None:
            from aira.profiler import launch
            launch.stop(server)
    print(f"model {note}")
    print("=== NEOX check:", "PASS" if all(r.values()) and len(r) == 5 else "FAIL", r)
    return 0 if all(r.values()) and len(r) == 5 else 1


if __name__ == "__main__":
    sys.exit(main())
