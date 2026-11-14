"""Browser control: Debian's Chromium driven by Playwright.

One Chromium with a persistent profile stays open between commands. Playwright's sync API is not
thread-safe and agents run on several threads, so every browser call is sent to one dedicated
thread. Headed when a display exists (the user sees the window), headless otherwise or when
AIRA_BROWSER_HEADLESS=1.
"""
from __future__ import annotations

import os
import queue
import shutil
import threading
import urllib.parse
from pathlib import Path
from typing import Any, Callable

SEARCH_URL = os.environ.get("AIRA_SEARCH_URL", "https://duckduckgo.com/?q={q}")
CHROMIUM = os.environ.get("AIRA_CHROMIUM", shutil.which("chromium") or "/usr/bin/chromium")


class BrowserError(RuntimeError):
    pass


class BrowserSession:
    def __init__(self, profile_dir: Path, headless: bool | None = None):
        self.profile_dir = profile_dir
        if headless is None:
            headless = os.environ.get("AIRA_BROWSER_HEADLESS") == "1" or not (
                os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        self.headless = headless
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ctx = None
        self._page = None

    # ---- the browser thread
    def _loop(self) -> None:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            while True:
                fn, box, done = self._q.get()
                if fn is None:
                    break
                try:
                    if self._ctx is None:
                        self.profile_dir.mkdir(parents=True, exist_ok=True)
                        self._ctx = p.chromium.launch_persistent_context(
                            str(self.profile_dir), executable_path=CHROMIUM, headless=self.headless,
                            args=["--no-first-run", "--no-default-browser-check", "--password-store=basic"],
                            viewport=None)
                        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
                    box["value"] = fn(self._page)
                except Exception as exc:
                    box["error"] = exc
                    if "closed" in str(exc).lower():   # the user closed the window: start fresh next time
                        self._ctx, self._page = None, None
                finally:
                    done.set()
            if self._ctx is not None:
                self._ctx.close()

    def _call(self, fn: Callable, timeout: float = 60) -> Any:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="aira-browser", daemon=True)
            self._thread.start()
        box: dict = {}
        done = threading.Event()
        self._q.put((fn, box, done))
        if not done.wait(timeout):
            raise BrowserError(f"the browser did not answer within {timeout:.0f}s")
        if "error" in box:
            raise BrowserError(str(box["error"]).splitlines()[0][:300])
        return box["value"]

    def close(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._q.put((None, None, None))
            self._thread.join(timeout=15)
        self._thread = None

    # ---- actions (each returns what the page now shows)
    @staticmethod
    def _state(page, text_chars: int = 2000) -> dict:
        try:
            text = page.inner_text("body", timeout=5000)
        except Exception:
            text = ""
        try:
            host = page.url.split("/")[2] if "//" in page.url else ""
            outbound = page.eval_on_selector_all(
                "a[href^='http']", "(as, h) => as.filter(a => !a.href.includes('://' + h + '/')).length", host)
        except Exception:
            outbound = 0
        return {"url": page.url, "title": page.title(), "text": " ".join(text.split())[:text_chars],
                "outbound_links": outbound}

    def open(self, url: str = "") -> dict:
        target = url or "about:blank"
        if url and not url.startswith(("https://", "http://", "about:")):
            target = "https://" + url
        return self._call(lambda pg: (pg.goto(target, wait_until="domcontentloaded", timeout=30000),
                                      pg.bring_to_front(), self._state(pg))[-1])

    def search(self, query: str) -> dict:
        url = SEARCH_URL.format(q=urllib.parse.quote_plus(query))

        def go(pg):
            pg.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                pg.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            pg.bring_to_front()
            st = self._state(pg)
            st["links"] = [{"text": " ".join(a.inner_text().split())[:120], "href": a.get_attribute("href")}
                           for a in pg.query_selector_all("a[href^='http']")[:40] if a.inner_text().strip()][:10]
            return st
        return self._call(go)

    def read(self) -> dict:
        return self._call(lambda pg: self._state(pg, 6000))

    def click(self, text: str) -> dict:
        def go(pg):
            pg.get_by_text(text, exact=False).first.click(timeout=10000)
            pg.wait_for_load_state("domcontentloaded", timeout=30000)
            return self._state(pg)
        return self._call(go)

    def back(self) -> dict:
        return self._call(lambda pg: (pg.go_back(wait_until="domcontentloaded", timeout=30000), self._state(pg))[-1])


def query_in_url(url: str, query: str) -> bool:
    """Read-back for a search: the page's own URL carries the query."""
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    got = " ".join(v for vals in qs.values() for v in vals).lower()
    return all(w in got for w in query.lower().split())
