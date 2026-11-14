#!/usr/bin/env python3
"""Phase 6 check: a ten-part task is faster with workers; a planted false sentence in a research
answer is flagged.

A. Ten searches over a generated notes folder (~3,000 files), run through the worker pool with
   1 worker and with the full pool (median of 3 timed runs after a warm-up). Results must be
   identical and the pool faster. The pheromone board must rank the faster approach first.
B. AIRA summarises a local paper with citations (summarize_document, checked per sentence). Two
   false sentences are planted into that answer -- a wrong number and an unrelated claim -- and
   both must be flagged while the check reports AIRA's own sentences.
Informational (not pass criteria): the ten searches as one request through AIRA, a notebook run,
live load-controller readings, and one arXiv search, web search and page fetch (generic query).
"""
from __future__ import annotations

import json
import os
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from fixture import build_fixture, start_server  # noqa: E402

FIXTURE = Path(tempfile.gettempdir()) / "aira-p6"
EMBED_PORT = 18081
TOPICS = ["gradient clipping", "learning rate warmup", "sparse attention", "mixed precision", "batch norm",
          "dropout schedule", "tokenizer bug", "checkpoint resume", "data leakage", "eval harness"]
PAPER = """# Block-Sparse Attention for Long Documents

## Abstract
We study block-sparse attention for documents longer than 16,000 tokens. Our model attends within
local windows of 512 tokens and to 64 global tokens. On the LongDoc benchmark it reaches 85.2%
accuracy, 3.1 points above a dense baseline of the same size.

## Method
Each layer splits the sequence into blocks of 512 tokens. A token attends to its own block, the two
neighbouring blocks, and 64 global tokens chosen at the start of every chapter. This keeps memory
linear in sequence length.

## Training
We trained a 350 million parameter model for 6 days on 8 GPUs, using a learning rate of 3e-4 with
2,000 warmup steps and a batch of 256 documents.

## Results
On LongDoc the model reaches 85.2% accuracy. The dense baseline reaches 82.1%. Memory use at
32,000 tokens is 9.5 GB, against 41 GB for dense attention. Removing the global tokens lowers
accuracy to 80.4%.

## Limitations
The global tokens are chosen by a fixed rule. Learned selection is left for future work.
"""
PLANTED = ["The model reaches 91.4% accuracy on the LongDoc benchmark.",
           "The authors also release a speech recognition model trained on 10,000 hours of podcasts."]


def make_corpus(root: Path, n: int = 3000) -> None:
    rng = random.Random(7)
    words = ("model loss run step epoch data note idea paper result baseline config seed metric plot "
             "table figure draft review meeting todo").split()
    for i in range(n):
        d = root / f"proj{i % 12}" / f"week{(i // 12) % 25}"
        d.mkdir(parents=True, exist_ok=True)
        body = " ".join(rng.choice(words) for _ in range(300))
        if i % 97 == 0:
            body += "\n" + TOPICS[(i // 97) % len(TOPICS)] + " observed in this run\n"
        (d / f"note{i}.md").write_text(f"# note {i}\n{body}\n")


def part_a(orch) -> bool:
    print("\n=== A. ten-part task with and without workers", flush=True)
    from aira.swarm.workers import WorkerPool
    from aira.tools.research import SEARCH_APPROACHES
    corpus = Path.home() / "notes" / "archive"
    make_corpus(corpus)
    print(f"corpus: {sum(1 for _ in corpus.rglob('*.md'))} files under {corpus}")
    approaches = {n: (lambda q, fn=fn: fn(corpus, q, 50)) for n, fn in SEARCH_APPROACHES.items()}
    pool = orch.pool
    # warm-up: page cache, and lets the pheromone board learn which approach is faster
    for name in approaches:
        t0 = time.monotonic()
        approaches[name](TOPICS[0])
        pool.board.deposit("text_search", name, True, time.monotonic() - t0)
    print("pheromone board:", json.dumps(pool.board.table("text_search")))
    seq_t, par_t = [], []
    for _ in range(3):
        t0 = time.monotonic()
        seq = pool.map("text_search", approaches, TOPICS, workers=1)
        seq_t.append(time.monotonic() - t0)
        t0 = time.monotonic()
        par = pool.map("text_search", approaches, TOPICS)
        par_t.append(time.monotonic() - t0)
    same = [[h["path"] for h in r.value] for r in seq] == [[h["path"] for h in r.value] for r in par]
    found = [len(r.value) for r in par]
    s, p = statistics.median(seq_t), statistics.median(par_t)
    used = sorted({r.approach for r in par})
    ranked = pool.board.ranked("text_search", list(approaches))
    print(f"results per topic: {found}; identical with and without workers: {same}; approach used: {used}")
    print(f"1 worker: {s:.2f}s (runs {', '.join(f'{x:.2f}' for x in seq_t)}); pool of {pool.size()}: {p:.2f}s "
          f"(runs {', '.join(f'{x:.2f}' for x in par_t)}); speed-up x{s / p:.1f}")
    print(f"pheromone ranking now: {ranked}")
    ok = same and all(found) and p < s and ranked[0] == used[0]
    print("PASS" if ok else "FAIL")
    # informational: the same ten-part task as one request through AIRA
    t0 = time.monotonic()
    turn = orch.handle("Search my notes archive folder ~/notes/archive for each of these: " + "; ".join(TOPICS) + ".")
    calls = [(s.tool, s.args.get("queries") and len(s.args["queries"])) for r in turn.runs for s in r.tool_calls]
    print(f"(info) through AIRA in {time.monotonic() - t0:.0f}s: calls {calls}\n(info) AIRA: {turn.reply[:300]!r}")
    return ok


def part_b(orch) -> bool:
    print("\n=== B. planted false sentence in a research answer", flush=True)
    from aira import citations
    from aira.tools.research import chunk_document
    paper = Path.home() / "papers" / "block-sparse-attention.md"
    paper.parent.mkdir(parents=True, exist_ok=True)
    paper.write_text(PAPER)
    turn = orch.handle("Summarise the paper ~/papers/block-sparse-attention.md with citations.")
    steps = [s for r in turn.runs for s in r.tool_calls]
    print(f"calls: {[(s.tool, s.args) for s in steps]}")
    print(f"AIRA: {turn.reply!r}")
    summ = next((s.result["data"] for s in steps if s.tool == "summarize_document" and s.result.get("ok")), None)
    if summ is None:
        print("FAIL: AIRA did not produce a checked summary")
        return False
    print(f"summary label: {summ['label']}; sentences: " + json.dumps(summ["sentences"]))
    own = [s["text"] for s in summ["sentences"]]
    chunks = chunk_document(PAPER)
    srcs = [citations.Source(i, f"{paper}#passage{i}", c) for i, c in enumerate(chunks, 1)]
    answer = " ".join(own[:2] + PLANTED[:1] + own[2:] + PLANTED[1:])
    checked = citations.check(answer, srcs)
    print("checked answer with planted sentences:")
    for s in checked.sentences:
        tag = "PLANTED " if any(p.rstrip(".") in s.text for p in PLANTED) else "AIRA    "
        print(f"  {tag}{'ok  ' if s.supported else 'FLAG'} cov={s.coverage:.2f} nums_ok={s.numbers_ok}  {s.text}")
    planted_rows = [s for s in checked.sentences if any(p.rstrip(".") in s.text for p in PLANTED)]
    # each planted sentence must be checked on its own (not merged into a neighbour) and flagged
    planted_flagged = len(planted_rows) == len(PLANTED) and all(
        not s.supported and s.text.rstrip(".") in [p.rstrip(".") for p in PLANTED] for s in planted_rows)
    own_rows = [s for s in checked.sentences if s not in planted_rows]
    own_ok = sum(1 for s in own_rows if s.supported)
    print(f"planted sentences flagged: {planted_flagged}; AIRA's own sentences supported: {own_ok}/{len(own_rows)}")
    print(f"rendered:\n{checked.render()}")
    ok = planted_flagged
    print("PASS" if ok else "FAIL")
    return ok


def part_info(orch) -> None:
    print("\n=== (info) notebook, load controller, web", flush=True)
    import nbformat
    nbdir = Path.home() / "notebooks"
    nbdir.mkdir(exist_ok=True)
    nb = nbformat.v4.new_notebook()
    nb.cells = [nbformat.v4.new_code_cell("import statistics\nprint(statistics.mean([82.1, 85.2, 80.4]))")]
    nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3", "language": "python"}
    nbformat.write(nb, nbdir / "means.ipynb")
    t = orch.handle("Run the notebook ~/notebooks/means.ipynb and tell me what it printed.")
    print(f"notebook: {[(s.tool, s.args) for r in t.runs for s in r.tool_calls]} -> {t.reply[:200]!r}")
    c = orch.controller
    for _ in range(3):
        c.tick()
    for h in c.history[-3:]:
        print(f"controller: {h['metrics']} healthy={h['healthy']} ({h['why']}) slots {h['slots']}")
    from aira.tools import research
    for name, fn in (("arxiv_search", lambda: research.arxiv_search("block sparse attention", 2)),
                     ("web_search", lambda: research.web_search("block sparse attention long documents", 2)),
                     ("fetch_page", lambda: research.fetch_page("https://example.org/"))):
        try:
            r = fn()
            print(f"{name}: ok {json.dumps(r)[:240]}")
        except Exception as exc:
            print(f"{name}: failed: {exc}")


def main() -> int:
    home = build_fixture(FIXTURE)
    env = os.environ
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), AIRA_DATA=str(FIXTURE / "aira-data"),
               AIRA_EMBED_SERVER=f"http://127.0.0.1:{EMBED_PORT}")
    env.pop("XDG_DATA_HOME", None)
    from aira.config import Config
    from aira.orchestrator import Orchestrator
    from aira.profiler import launch
    embed = launch.start_embedder(EMBED_PORT, Path(tempfile.gettempdir()) / "aira-p6-embed.log")
    server, note = (None, env["AIRA_SERVER"]) if "AIRA_SERVER" in env else start_server(19000, "aira-p6-server.log")
    asked = []
    try:
        orch = Orchestrator(Config(), lambda a, tool, args, risks: asked.append(tool) or False)
        print(f"model {note}; worker pool size {orch.pool.size()}; active slots {orch.controller.slots}")
        results = {"A_ten_part_faster": part_a(orch), "B_planted_flagged": part_b(orch)}
        part_info(orch)
    finally:
        launch.stop(embed)
        if server is not None:
            launch.stop(server)
    print(f"\nconfirmation requests through AIRA (all denied): {asked}")
    print("=== phase 6:", "PASS" if all(results.values()) else "FAIL", results)
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
