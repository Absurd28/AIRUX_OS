"""Research agent tools: local notes and papers, datasets, arXiv and web search, page fetching,
cited document summaries (checked sentence by sentence, layer 8) and Jupyter notebooks.

Several searches at once go to the worker pool: each query is a part, approaches are a grep
subprocess or a pure-Python walk, chosen by the pheromone board.
"""
from __future__ import annotations

import csv
import html
import html.parser
import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from .. import citations
from .base import I, Registry, Risk, S, Tool, ToolError, exists_dir, exists_path, expand, run, truncate

NOTE_EXT = {".md", ".txt", ".org", ".rst", ".tex", ".py", ".ipynb"}
UA = {"User-Agent": "aira/0.1 (+local research assistant)"}


# ---------------------------------------------------------------- local notes
def _terms(query: str) -> list[str]:
    return [t.lower() for t in query.split() if t]


def _snippet(path: Path, terms: list[str]) -> dict | None:
    try:
        if path.suffix.lower() not in NOTE_EXT or path.stat().st_size > 2_000_000:
            return None
        text = path.read_text(errors="replace")
    except OSError:
        return None
    low = text.lower()
    if not all(t in low for t in terms):
        return None
    i = low.find(terms[0])
    return {"path": str(path), "line": low.count("\n", 0, i) + 1,
            "snippet": text[max(0, i - 120): i + 200].replace("\n", " ")}


def search_python(base: Path, query: str, max_results: int = 20) -> list[dict]:
    terms, hits = _terms(query), []
    for dirpath, dirnames, files in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for f in files:
            h = _snippet(Path(dirpath) / f, terms)
            if h:
                hits.append(h)
                if len(hits) >= max_results:
                    return hits
    return hits


def search_grep(base: Path, query: str, max_results: int = 20) -> list[dict]:
    terms = _terms(query)
    exe = shutil.which("grep")
    if not exe:
        raise ToolError("grep not available")
    cmd = [exe, "-rIl", "-i", "-F", "--exclude-dir=.*"] + [f"--include=*{e}" for e in NOTE_EXT] + ["-e", terms[0],
                                                                                                    str(base)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode not in (0, 1):
        raise ToolError(f"grep failed: {r.stderr.strip()[:200]}")
    hits = []
    for line in sorted(r.stdout.splitlines()):
        h = _snippet(Path(line), terms)
        if h:
            hits.append(h)
            if len(hits) >= max_results:
                break
    return hits


SEARCH_APPROACHES = {"grep": search_grep, "python": search_python}


def make_search_notes(pool):
    def search_notes(query: str = "", folder: str = "~", max_results: int = 20, queries: list | None = None) -> dict:
        base = expand(folder)
        if not base.is_dir():
            raise ToolError(f"no such folder: {base}")
        qs = [q for q in (queries or []) if q.strip()] or ([query] if query.strip() else [])
        if not qs:
            raise ToolError("empty query")
        approaches = {name: (lambda q, fn=fn: fn(base, q, max_results)) for name, fn in SEARCH_APPROACHES.items()}
        if pool is None:
            parts = [{"query": q, "results": search_grep(base, q, max_results)} for q in qs]
            return parts[0] | {"folder": str(base)} if len(parts) == 1 else {"folder": str(base), "searches": parts}
        res = pool.map("text_search", approaches, qs)
        failed = [r for r in res if not r.ok]
        if failed and len(failed) == len(res):
            raise ToolError("; ".join(r.error for r in failed))
        parts = [{"query": r.part, "results": r.value if r.ok else [], "approach": r.approach,
                  **({"error": r.error} if not r.ok else {})} for r in res]
        if len(parts) == 1:
            return {"query": qs[0], "folder": str(base), "results": parts[0]["results"],
                    "approach": parts[0]["approach"]}
        return {"folder": str(base), "searches": parts}
    return search_notes


# ---------------------------------------------------------------- documents and data
def document_text(p: Path) -> str:
    if p.suffix.lower() == ".pdf":
        if not shutil.which("pdftotext"):
            raise ToolError("pdftotext is not installed (package poppler-utils)")
        return run(["pdftotext", "-layout", str(p), "-"], timeout=120)
    return p.read_text(errors="replace")


def read_document(path: str, max_chars: int = 12000) -> dict:
    p = expand(path)
    if not p.is_file():
        raise ToolError(f"no such file: {p}")
    text = document_text(p)
    pages = text.count("\f") + 1 if p.suffix.lower() == ".pdf" else None
    return {"path": str(p), "pages": pages, "chars": len(text), "text": truncate(text, max_chars)}


def dataset_info(path: str, preview_rows: int = 5) -> dict:
    p = expand(path)
    if not p.is_file():
        raise ToolError(f"no such file: {p}")
    if p.suffix.lower() not in (".csv", ".tsv"):
        raise ToolError("only .csv and .tsv are supported for now")
    delim = "\t" if p.suffix.lower() == ".tsv" else ","
    with p.open(newline="", errors="replace") as f:
        reader = csv.reader(f, delimiter=delim)
        header = next(reader, None)
        if header is None:
            raise ToolError("file is empty")
        rows, n = [], 0
        numeric = [True] * len(header)
        for row in reader:
            n += 1
            if len(rows) < preview_rows:
                rows.append(row)
            for i, v in enumerate(row[: len(header)]):
                if numeric[i] and v.strip():
                    try:
                        float(v)
                    except ValueError:
                        numeric[i] = False
    return {"path": str(p), "rows": n, "columns": header,
            "numeric_columns": [h for h, num in zip(header, numeric) if num], "preview": rows}


# ---------------------------------------------------------------- the web (always confirmed)
ARXIV_API = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"


def arxiv_search(query: str, max_results: int = 5) -> dict:
    url = ARXIV_API + "?" + urllib.parse.urlencode(
        {"search_query": f"all:{query}", "start": 0, "max_results": max_results, "sortBy": "relevance"})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
            root = ET.fromstring(r.read())
    except Exception as exc:
        raise ToolError(f"arXiv search failed: {exc}") from exc
    papers = []
    for e in root.findall(f"{ATOM}entry"):
        papers.append({
            "title": " ".join((e.findtext(f"{ATOM}title") or "").split()),
            "authors": [a.findtext(f"{ATOM}name") for a in e.findall(f"{ATOM}author")][:6],
            "published": (e.findtext(f"{ATOM}published") or "")[:10],
            "url": e.findtext(f"{ATOM}id"),
            "summary": truncate(" ".join((e.findtext(f"{ATOM}summary") or "").split()), 600),
        })
    return {"query": query, "source": ARXIV_API, "papers": papers}


class _TextExtractor(html.parser.HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self):
        super().__init__()
        self.parts, self.links, self._skip, self._href = [], [], 0, None
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag == "a":
            self._href = dict(attrs).get("href")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in ("p", "div", "br", "li", "h1", "h2", "h3", "tr"):
            self.parts.append("\n")
        if tag == "a":
            self._href = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if not self._skip:
            self.parts.append(data)
            if self._href and data.strip():
                self.links.append((data.strip(), self._href))

    def text(self) -> str:
        return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", "".join(self.parts))).strip()


def _get(url: str, timeout: float = 30) -> tuple[str, str]:
    if not url.startswith(("https://", "http://")):
        raise ToolError("only http and https addresses can be fetched")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
            ctype = r.headers.get("Content-Type", "")
            raw = r.read(3_000_000)
    except Exception as exc:
        raise ToolError(f"could not fetch {url}: {exc}") from exc
    charset = re.search(r"charset=([\w-]+)", ctype)
    return raw.decode(charset.group(1) if charset else "utf-8", errors="replace"), ctype


def _strip(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _real_url(href: str) -> str:
    href = html.unescape(href)
    q = urllib.parse.urlparse(href)
    if "uddg=" in (q.query or ""):
        return urllib.parse.parse_qs(q.query)["uddg"][0]
    return href


def parse_ddg_html(body: str) -> list[dict]:
    out = []
    for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
                         r'class="result__snippet"[^>]*>(.*?)</a>', body, re.S):
        out.append({"title": _strip(m.group(2)), "url": _real_url(m.group(1)), "snippet": _strip(m.group(3))})
    return out


def parse_ddg_lite(body: str) -> list[dict]:
    links = re.findall(r"""<a[^>]+href=["']([^"']+)["'][^>]*class=['"]result-link['"][^>]*>(.*?)</a>""", body, re.S)
    snippets = re.findall(r"""class=['"]result-snippet['"][^>]*>(.*?)</td>""", body, re.S)
    return [{"title": _strip(t), "url": _real_url(h), "snippet": _strip(snippets[i]) if i < len(snippets) else ""}
            for i, (h, t) in enumerate(links)]


def web_search(query: str, max_results: int = 5) -> dict:
    """DuckDuckGo's HTML results (no API key), then its lite page. The query leaves the machine.
    DuckDuckGo sometimes answers automated clients with a bot check; AIRA reports that plainly
    and never tries to get around it."""
    q = urllib.parse.urlencode({"q": query})
    bot_check = False
    for url, parse in (("https://html.duckduckgo.com/html/?" + q, parse_ddg_html),
                       ("https://lite.duckduckgo.com/lite/?" + q, parse_ddg_lite)):
        body, _ = _get(url)
        results = parse(body)[:max_results]
        if results:
            return {"query": query, "source": url.split("/")[2], "results": results}
        bot_check = bot_check or bool(re.search(r"anomaly|challenge|captcha", body, re.I))
    if bot_check:
        raise ToolError("DuckDuckGo answered with a bot check instead of results; try again later")
    raise ToolError("the search pages returned no results (their format may have changed)")


def fetch_page(url: str, max_chars: int = 8000) -> dict:
    body, ctype = _get(url)
    if "html" not in ctype and "<html" not in body[:500].lower():
        return {"url": url, "title": "", "text": truncate(body, max_chars)}
    p = _TextExtractor()
    p.feed(body)
    return {"url": url, "title": p.title.strip(), "text": truncate(p.text(), max_chars)}


# ---------------------------------------------------------------- cited summaries (layer 8)
SUMMARY_SCHEMA = {"type": "object", "properties": {"sentences": {"type": "array", "minItems": 1, "maxItems": 6,
                  "items": {"type": "object", "properties": {"text": {"type": "string"},
                                                             "passage": {"type": "integer", "minimum": 1}},
                            "required": ["text", "passage"], "additionalProperties": False}}},
                  "required": ["sentences"], "additionalProperties": False}


def chunk_document(text: str, max_chars: int = 900, max_chunks: int = 8, focus: str = "") -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if len(cur) + len(p) > max_chars and cur:
            chunks.append(cur)
            cur = ""
        cur = (cur + "\n" + p).strip()
    if cur:
        chunks.append(cur)
    if focus and len(chunks) > max_chunks:
        fw = citations.content_words(focus)
        chunks.sort(key=lambda c: -len(fw & citations.content_words(c)))
    return chunks[:max_chunks]


def make_summarize(llm):
    def summarize_document(path: str, focus: str = "") -> dict:
        if llm is None:
            raise ToolError("no model available for summaries")
        p = expand(path)
        if not p.is_file():
            raise ToolError(f"no such file: {p}")
        chunks = chunk_document(document_text(p), focus=focus)
        if not chunks:
            raise ToolError(f"{p} has no text")
        numbered = "\n\n".join(f"[{i}] {c}" for i, c in enumerate(chunks, 1))
        msgs = [{"role": "system", "content": "Summarise the document in at most 6 short sentences. Every sentence "
                 "must state only what its numbered passage says, with the same numbers. Give the passage number "
                 "for each sentence."},
                {"role": "user", "content": (f"Focus: {focus}\n\n" if focus else "") + numbered}]
        data = llm.chat(msgs, SUMMARY_SCHEMA, max_tokens=500).data or {}
        # the citation goes inside the sentence ("... text [2].") so it stays with its own sentence
        answer = " ".join(f"{s['text'].strip().rstrip('.')} [{s['passage']}]." for s in data.get("sentences", []))
        srcs = [citations.Source(i, f"{p}#passage{i}", c) for i, c in enumerate(chunks, 1)]
        checked = citations.check(answer, srcs)
        return {"path": str(p), "label": checked.label, "summary": checked.render(),
                "unsupported": [s.text for s in checked.unsupported],
                "sentences": [{"text": s.text, "supported": s.supported, "coverage": s.coverage}
                              for s in checked.sentences]}
    return summarize_document


# ---------------------------------------------------------------- Jupyter
def jupyter(action: str, path: str = "", timeout: int = 300) -> dict:
    if action == "kernels":
        from jupyter_client.kernelspec import KernelSpecManager
        return {"kernels": sorted(KernelSpecManager().find_kernel_specs())}
    import nbformat
    from nbclient import NotebookClient
    from nbclient.exceptions import CellExecutionError
    p = expand(path)
    if not p.is_file() or p.suffix != ".ipynb":
        raise ToolError(f"no such notebook: {p}")
    nb = nbformat.read(p, as_version=4)
    out = p.with_name(p.stem + ".executed.ipynb")
    error = None
    try:
        NotebookClient(nb, timeout=timeout, kernel_name=nb.metadata.get("kernelspec", {}).get("name", "python3"),
                       resources={"metadata": {"path": str(p.parent)}}).execute()
    except CellExecutionError as exc:
        error = str(exc).strip().splitlines()[-1][:300]
    nbformat.write(nb, out)
    cells = []
    for i, c in enumerate(nb.cells):
        if c.cell_type != "code":
            continue
        texts = []
        for o in c.get("outputs", []):
            if o.get("output_type") == "stream":
                texts.append(o.get("text", ""))
            elif o.get("output_type") in ("execute_result", "display_data"):
                texts.append(o.get("data", {}).get("text/plain", ""))
            elif o.get("output_type") == "error":
                texts.append(f"{o.get('ename')}: {o.get('evalue')}")
        cells.append({"cell": i, "output": truncate("".join(texts).strip(), 500)})
    return {"notebook": str(p), "executed_copy": str(out), "error": error, "cells": cells}


def _jupyter_readback(a: dict, result) -> tuple[bool, str]:
    if a.get("action") != "run":
        return True, "read only"
    out = Path(result["executed_copy"])
    return out.is_file(), f"{out} written" if out.is_file() else f"{out} missing"


def register(reg: Registry, pool=None, llm=None) -> None:
    A = "research"
    reg.add(Tool("search_notes", A, "search local notes, papers' text files and code for words; several "
                 "searches at once with queries",
                 {"query": S("words to find"), "folder": S("folder to search, default ~"),
                  "max_results": I("limit per search", 1, 100),
                  "queries": {"type": "array", "items": {"type": "string"}, "maxItems": 20,
                              "description": "several searches at once"}}, [], make_search_notes(pool),
                 entities=lambda a: [exists_dir(a["folder"])] if a.get("folder") else []))
    reg.add(Tool("read_document", A, "read the text of a PDF, Markdown or text document",
                 {"path": S("document path"), "max_chars": I("limit", 100, 50000)}, ["path"], read_document,
                 entities=lambda a: [exists_path(a["path"], "document")]))
    reg.add(Tool("dataset_info", A, "rows (not counting the header), columns and a preview of a CSV/TSV dataset",
                 {"path": S("dataset path"), "preview_rows": I("rows to show", 0, 50)}, ["path"], dataset_info,
                 entities=lambda a: [exists_path(a["path"], "dataset")]))
    reg.add(Tool("summarize_document", A, "summarise a local PDF or text document with a citation per sentence; "
                 "unsupported sentences are flagged",
                 {"path": S("document path"), "focus": S("what to focus on (optional)")}, ["path"],
                 make_summarize(llm), entities=lambda a: [exists_path(a["path"], "document")]))
    reg.add(Tool("arxiv_search", A, "search arXiv for papers (sends the query to arxiv.org)",
                 {"query": S("search terms"), "max_results": I("how many", 1, 25)}, ["query"], arxiv_search,
                 frozenset({Risk.SEND_OFF})))
    reg.add(Tool("web_search", A, "search the web (sends the query to duckduckgo.com)",
                 {"query": S("search terms"), "max_results": I("how many", 1, 10)}, ["query"], web_search,
                 frozenset({Risk.SEND_OFF})))
    reg.add(Tool("fetch_page", A, "read the text of a web page (contacts that website)",
                 {"url": S("http or https address"), "max_chars": I("limit", 500, 50000)}, ["url"], fetch_page,
                 frozenset({Risk.SEND_OFF})))
    reg.add(Tool("jupyter", A, "run a Jupyter notebook into a new .executed.ipynb copy and report its outputs, "
                 "or list kernels", {"action": S("what to do", enum=["run", "kernels"]), "path": S("notebook path")},
                 ["action"], jupyter, lambda a: {Risk.WRITE} if a.get("action") == "run" else {Risk.READ},
                 entities=lambda a: [exists_path(a.get("path", ""), "notebook")] if a.get("action") == "run" else [],
                 readback=_jupyter_readback, changes_state=lambda a: a.get("action") == "run"))
