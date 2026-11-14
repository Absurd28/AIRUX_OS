"""Embeddings from a llama-server running a small embedding model (nomic-embed-text v1.5).

nomic is Matryoshka-trained: the first 256 of 768 dimensions are kept and re-normalised,
which quarters vector storage. Queries and documents use nomic's task prefixes.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import urllib.error
import urllib.request

DIM = 256


class EmbedError(RuntimeError):
    pass


def _normalise(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class Embedder:
    def __init__(self, url: str | None = None, timeout: float = 30):
        self.url = (url or os.environ.get("AIRA_EMBED_SERVER", "http://127.0.0.1:8081")).rstrip("/")
        self.timeout = timeout

    def _embed(self, texts: list[str]) -> list[list[float]]:
        body = json.dumps({"input": texts, "model": "nomic-embed-text-v1.5"}).encode()
        req = urllib.request.Request(self.url + "/v1/embeddings", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read())["data"]
        except (urllib.error.URLError, TimeoutError, ConnectionError, KeyError, ValueError) as exc:
            raise EmbedError(f"embedding server unavailable at {self.url}: {exc}") from exc
        data.sort(key=lambda d: d["index"])
        return [_normalise(d["embedding"][:DIM]) for d in data]

    def documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(["search_document: " + t for t in texts])

    def query(self, text: str) -> list[float]:
        return self._embed(["search_query: " + text])[0]


class HashEmbedder:
    """Deterministic bag-of-words embedder for tests: shared words -> similar vectors."""

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * DIM
        for w in text.lower().replace(".", " ").replace(",", " ").split():
            h = int(hashlib.sha256(w.encode()).hexdigest(), 16)
            v[h % DIM] += 1.0
        return _normalise(v)

    def documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def query(self, text: str) -> list[float]:
        return self._vec(text)
