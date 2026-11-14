"""Research agent tools: local notes and papers, datasets, arXiv search.

Web search, PDF summaries and Jupyter control are added in phase 6.
"""
from __future__ import annotations

import csv
import os
import shutil
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from .base import I, Registry, Risk, S, Tool, ToolError, expand, run, truncate

NOTE_EXT = {".md", ".txt", ".org", ".rst", ".tex", ".py", ".ipynb"}


def search_notes(query: str, folder: str = "~", max_results: int = 20) -> dict:
    base = expand(folder)
    if not base.is_dir():
        raise ToolError(f"no such folder: {base}")
    terms = [t.lower() for t in query.split() if t]
    if not terms:
        raise ToolError("empty query")
    hits = []
    for dirpath, dirnames, files in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for f in files:
            p = Path(dirpath) / f
            if p.suffix.lower() not in NOTE_EXT or p.stat().st_size > 2_000_000:
                continue
            text = p.read_text(errors="replace")
            low = text.lower()
            if all(t in low for t in terms):
                i = low.find(terms[0])
                lineno = low.count("\n", 0, i) + 1
                snippet = text[max(0, i - 120): i + 200].replace("\n", " ")
                hits.append({"path": str(p), "line": lineno, "snippet": snippet})
                if len(hits) >= max_results:
                    return {"query": query, "folder": str(base), "results": hits, "truncated": True}
    return {"query": query, "folder": str(base), "results": hits, "truncated": False}


def read_document(path: str, max_chars: int = 12000) -> dict:
    p = expand(path)
    if not p.is_file():
        raise ToolError(f"no such file: {p}")
    if p.suffix.lower() == ".pdf":
        if not shutil.which("pdftotext"):
            raise ToolError("pdftotext is not installed (package poppler-utils)")
        text = run(["pdftotext", "-layout", str(p), "-"], timeout=120)
        pages = text.count("\f") + 1
    else:
        text = p.read_text(errors="replace")
        pages = None
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


ARXIV_API = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"


def arxiv_search(query: str, max_results: int = 5) -> dict:
    url = ARXIV_API + "?" + urllib.parse.urlencode(
        {"search_query": f"all:{query}", "start": 0, "max_results": max_results, "sortBy": "relevance"})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "aira/0.1"}),
                                    timeout=30) as r:
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


def register(reg: Registry) -> None:
    A = "research"
    reg.add(Tool("search_notes", A, "search local notes, papers' text files and code for words",
                 {"query": S("words to find"), "folder": S("folder to search, default ~"),
                  "max_results": I("limit", 1, 100)}, ["query"], search_notes))
    reg.add(Tool("read_document", A, "read the text of a PDF, Markdown or text document",
                 {"path": S("document path"), "max_chars": I("limit", 100, 50000)}, ["path"], read_document))
    reg.add(Tool("dataset_info", A, "rows, columns and a preview of a CSV/TSV dataset",
                 {"path": S("dataset path"), "preview_rows": I("rows to show", 0, 50)}, ["path"], dataset_info))
    reg.add(Tool("arxiv_search", A, "search arXiv for papers (sends the query to arxiv.org)",
                 {"query": S("search terms"), "max_results": I("how many", 1, 25)}, ["query"], arxiv_search,
                 frozenset({Risk.SEND_OFF})))
