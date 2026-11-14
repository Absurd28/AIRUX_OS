"""Swarm, research checker and research tool tests (no model server needed)."""
from __future__ import annotations

import datetime as dt
import http.server
import json
import threading
import time

import pytest

from aira import citations
from aira.swarm.controller import LoadController, Metrics, read_memory_pressure
from aira.swarm.pheromones import UNTRIED, PheromoneBoard
from aira.swarm.quorum import quorum
from aira.swarm.workers import WorkerPool
from aira.tools import research


class Clock:
    def __init__(self):
        self.t = dt.datetime(2026, 11, 14, 12, 0)

    def __call__(self):
        return self.t


@pytest.fixture
def board(tmp_path):
    c = Clock()
    b = PheromoneBoard(tmp_path / "ph.db", clock=c)
    b.c = c
    return b


# ---------- pheromone board ----------

def test_board_scores_rank_and_decay(board):
    assert board.score("search", "grep") == UNTRIED
    board.deposit("search", "grep", True, 0.0)          # +1
    board.deposit("search", "python", True, 3.0)        # +0.25
    board.deposit("search", "broken", False, 0.1)       # -1
    assert board.ranked("search", ["broken", "python", "grep", "new"]) == ["grep", "python", "new", "broken"]
    before = board.score("search", "grep")
    board.c.t += dt.timedelta(days=2)
    assert board.score("search", "grep") == pytest.approx(before * 0.9 ** 2)
    row = [r for r in board.table("search") if r["approach"] == "grep"][0]
    assert row["successes"] == 1 and row["failures"] == 0


# ---------- worker pool ----------

def test_pool_falls_back_and_learns(board):
    calls = []

    def bad(p):
        calls.append(("bad", p))
        raise RuntimeError("nope")

    def good(p):
        calls.append(("good", p))
        return p * 2

    pool = WorkerPool(board, workers=1)
    board.deposit("double", "bad", True, 0)               # bad looks best at first
    r = pool.map("double", {"bad": bad, "good": good}, [1])[0]
    assert r.ok and r.value == 2 and r.approach == "good" and r.tried == ["bad", "good"]
    calls.clear()
    pool.map("double", {"bad": bad, "good": good}, [2])     # after the failure, good is tried first
    assert calls == [("good", 2)]


def test_pool_runs_parts_in_parallel(board):
    def slow(p):
        time.sleep(0.2)
        return p

    pool = WorkerPool(board, workers=8)
    t0 = time.monotonic()
    seq = pool.map("sleep", {"slow": slow}, list(range(10)), workers=1)
    t_seq = time.monotonic() - t0
    t0 = time.monotonic()
    par = pool.map("sleep", {"slow": slow}, list(range(10)))
    t_par = time.monotonic() - t0
    assert [r.value for r in par] == [r.value for r in seq] == list(range(10))
    assert t_par < t_seq / 3


def test_pool_respects_controller_limit(board):
    limit = {"n": 2}
    pool = WorkerPool(board, workers=8, limit=lambda: limit["n"])
    assert pool.size() == 2
    limit["n"] = 0
    assert pool.size() == 1


# ---------- quorum ----------

def test_quorum():
    q = quorum({"a": lambda: [2, 1], "b": lambda: [1, 2], "c": lambda: [3]}, need=2)
    assert q.agreed and sorted(q.value) == [1, 2] and q.support == 2
    q = quorum({"a": lambda: 1, "b": lambda: 2, "c": lambda: 1 / 0}, need=2)
    assert not q.agreed and q.value is None and q.votes["c"].startswith("error")


# ---------- load controller ----------

def test_controller_adds_one_and_halves():
    m = {"v": Metrics(1.0, 60.0, 5.0)}
    c = LoadController(max_slots=6, slots=1, read=lambda: m["v"])
    assert [c.tick() for _ in range(7)] == [2, 3, 4, 5, 6, 6, 6]
    m["v"] = Metrics(25.0, 60.0, 5.0)        # memory pressure
    assert c.tick() == 3 and c.tick() == 1 and c.tick() == 1
    m["v"] = Metrics(1.0, 92.0, 5.0)         # too hot
    assert c.tick() == 1
    assert "temperature" in c.history[-1]["why"]
    m["v"] = Metrics(None, None, None)       # nothing measurable counts as healthy
    assert c.tick() == 2


def test_controller_latency_median():
    c = LoadController(max_slots=3, slots=3)
    for s in (1, 50, 2, 3, 40):
        c.record_latency(s)
    assert c.median_latency() == 3


def test_read_memory_pressure(tmp_path):
    f = tmp_path / "memory"
    f.write_text("some avg10=12.50 avg60=3.00 avg300=1.00 total=1\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
    assert read_memory_pressure(f) == 12.5
    assert read_memory_pressure(tmp_path / "missing") is None


# ---------- research checker (layer 8) ----------

PAPER = ("Sparse attention reduces the cost of long sequences. Our model uses block sparse attention "
         "with a window of 512 tokens. On the long-document benchmark it reaches 85.2% accuracy, "
         "3.1 points above the dense baseline. Training took 6 days on 8 GPUs.")


def test_checker_supported_and_planted_false():
    src = [citations.Source(1, "~/papers/sparse.md", PAPER)]
    ok = citations.check("The model uses block sparse attention with a 512-token window [1].", src)
    assert ok.label == "verified" and ok.sentences[0].supported
    planted = citations.check("It reaches 85.2% accuracy on the long-document benchmark [1]. "
                              "Training took 6 days on 512 GPUs [1]. "
                              "The authors also released a speech recognition model trained on podcasts [1].", src)
    flags = [s.supported for s in planted.sentences]
    assert flags == [True, False, False]   # wrong number, and an unrelated claim
    assert planted.label == "partly unverified" and planted.render().count("[unsupported]") == 2


def test_checker_wrong_number_with_same_words():
    src = [citations.Source(1, "p", PAPER)]
    assert not citations.check("On the long-document benchmark it reaches 95.2% accuracy.", src).sentences[0].supported


def test_checker_no_source_is_unverified():
    c = citations.check("Transformers were introduced in 2017.", [])
    assert c.label == "unverified" and c.render().startswith("Unverified (no source)")


def test_checker_uses_cited_source_only():
    a = citations.Source(1, "a", "Cats sleep sixteen hours a day.")
    b = citations.Source(2, "b", PAPER)
    c = citations.check("Our model uses block sparse attention with a window of 512 tokens [1].", [a, b])
    assert not c.sentences[0].supported            # cited [1], which does not say it
    c = citations.check("Our model uses block sparse attention with a window of 512 tokens.", [a, b])
    assert c.sentences[0].supported                # uncited: any source may support it


# ---------- research tools ----------

@pytest.fixture
def corpus(tmp_path):
    base = tmp_path / "notes"
    for i in range(60):
        d = base / f"d{i % 6}"
        d.mkdir(parents=True, exist_ok=True)
        extra = "transformer divergence at step 400" if i % 20 == 3 else "nothing here"
        (d / f"n{i}.md").write_text(f"note {i}\n{extra}\n")
    (base / ".hidden").mkdir()
    (base / ".hidden" / "x.md").write_text("transformer divergence hidden\n")
    return base


def test_search_approaches_agree(corpus):
    g = research.search_grep(corpus, "transformer divergence")
    p = research.search_python(corpus, "transformer divergence")
    assert sorted(h["path"] for h in g) == sorted(h["path"] for h in p)
    assert len(g) == 3 and all(".hidden" not in h["path"] for h in g)
    q = quorum({"grep": lambda: [h["path"] for h in research.search_grep(corpus, "divergence step")],
                "python": lambda: [h["path"] for h in research.search_python(corpus, "divergence step")]})
    assert q.agreed and len(q.value) == 3


def test_search_notes_many_queries_through_pool(corpus, board):
    fn = research.make_search_notes(WorkerPool(board, workers=4))
    out = fn(folder=str(corpus), queries=["transformer", "note 42", "zzz-none"])
    counts = [len(s["results"]) for s in out["searches"]]
    assert counts == [3, 1, 0] and all(s["approach"] in ("grep", "python") for s in out["searches"])
    assert board.table("text_search")


def test_fetch_page_extracts_text():
    page = (b"<html><head><title>Sparse attention</title><style>x{}</style></head><body><h1>Results</h1>"
            b"<p>Accuracy was 85.2%.</p><script>evil()</script><a href='/next'>next page</a></body></html>")

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = research.fetch_page(f"http://127.0.0.1:{srv.server_port}/p")
    finally:
        srv.shutdown()
    assert r["title"] == "Sparse attention" and "Accuracy was 85.2%." in r["text"]
    assert "evil" not in r["text"] and "x{}" not in r["text"]
    with pytest.raises(research.ToolError):
        research.fetch_page("file:///etc/passwd")


def test_web_search_lite_fallback_and_bot_check(monkeypatch):
    lite = ("<a rel=\"nofollow\" href=\"//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fa&amp;rut=1\" "
            "class='result-link'>Result A</a></td></tr><tr><td class='result-snippet'>About <b>A</b>.</td>")
    pages = {"html.duckduckgo.com": "<html>anomaly detected</html>", "lite.duckduckgo.com": lite}
    monkeypatch.setattr(research, "_get", lambda url, timeout=30: (pages[url.split("/")[2]], "text/html"))
    r = research.web_search("x")
    assert r["source"] == "lite.duckduckgo.com"
    assert r["results"] == [{"title": "Result A", "url": "https://example.org/a", "snippet": "About A."}]
    pages["lite.duckduckgo.com"] = "<html>please complete the challenge</html>"
    with pytest.raises(research.ToolError, match="bot check"):
        research.web_search("x")


def test_summary_citation_stays_with_its_sentence():
    class LLM:
        def chat(self, msgs, schema, max_tokens=0):
            from aira.llm import Completion
            return Completion("", {"sentences": [{"text": "Cats sleep a lot.", "passage": 1},
                                                 {"text": "Dogs bark at 3 am", "passage": 2}]}, 0, 0, 0, 0)
    import tempfile
    from pathlib import Path
    p = Path(tempfile.mkdtemp()) / "doc.md"
    p.write_text("Cats sleep a lot every day.\n\nDogs bark at 3 am in the city.\n")
    r = research.make_summarize(LLM())(str(p))
    assert [s["text"] for s in r["sentences"]] == ["Cats sleep a lot [1].", "Dogs bark at 3 am [2]."]
    assert r["label"] == "verified"


def test_web_search_parses_results(monkeypatch):
    html = ('<div><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Farxiv.org%2Fabs%2F1'
            '&amp;rut=x">Sparse <b>Attention</b></a><a class="result__snippet" href="x">Long <b>docs</b>.</a></div>')
    monkeypatch.setattr(research, "_get", lambda url, timeout=30: (html, "text/html"))
    r = research.web_search("sparse attention")
    assert r["results"] == [{"title": "Sparse Attention", "url": "https://arxiv.org/abs/1", "snippet": "Long docs."}]


def test_jupyter_runs_notebook(tmp_path):
    import nbformat
    nb = nbformat.v4.new_notebook()
    nb.cells = [nbformat.v4.new_code_cell("x = 21 * 2\nprint(x)"), nbformat.v4.new_code_cell("x + 1")]
    nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3", "language": "python"}
    p = tmp_path / "calc.ipynb"
    nbformat.write(nb, p)
    r = research.jupyter("run", str(p))
    assert r["error"] is None and [c["output"] for c in r["cells"]] == ["42", "43"]
    assert (tmp_path / "calc.executed.ipynb").exists()
    assert research._jupyter_readback({"action": "run"}, r)[0]


def test_chunk_document():
    text = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(20))
    chunks = research.chunk_document(text, max_chars=900, max_chunks=8)
    assert 1 < len(chunks) <= 8 and all(len(c) < 1400 for c in chunks)
