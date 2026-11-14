"""Client for llama-server's OpenAI-compatible chat endpoint (stdlib only).

Every structured call passes a JSON schema as `response_format`; llama-server compiles it to a
grammar, so the model can only produce JSON that matches (grounding layer 1).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


class LLMError(RuntimeError):
    pass


@dataclass
class Completion:
    content: str
    data: dict | None
    prompt_tokens: int
    cached_tokens: int
    gen_tokens: int
    seconds: float


class LLM:
    def __init__(self, base_url: str, timeout: float = 300.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.on_latency = None  # set by the load controller: called with seconds per request

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(self.base_url + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            raise LLMError(f"model server returned HTTP {exc.code}: {exc.read()[:300]!r}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise LLMError(f"model server unreachable at {self.base_url}: {exc}") from exc

    def total_slots(self) -> int:
        try:
            with urllib.request.urlopen(self.base_url + "/props", timeout=10) as r:
                return int(json.loads(r.read()).get("total_slots", 1))
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as exc:
            raise LLMError(f"model server unreachable at {self.base_url}: {exc}") from exc

    def chat(self, messages: list[dict], schema: dict | None = None, *, slot: int | None = None,
             temperature: float = 0.0, max_tokens: int = 512, seed: int | None = None) -> Completion:
        body: dict = {"messages": messages, "temperature": temperature, "max_tokens": max_tokens,
                      "cache_prompt": True}
        if schema is not None:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "step", "schema": schema}}
        if slot is not None:
            body["id_slot"] = slot
        if seed is not None:
            body["seed"] = seed
        t0 = time.monotonic()
        r = self._post("/v1/chat/completions", body)
        if self.on_latency is not None:
            self.on_latency(time.monotonic() - t0)
        try:
            msg = r["choices"][0]["message"]
            content = msg.get("content") or ""
        except (KeyError, IndexError) as exc:
            raise LLMError(f"malformed server response: {str(r)[:300]}") from exc
        data = None
        if schema is not None:
            try:
                data = json.loads(content)
            except json.JSONDecodeError as exc:
                # with a grammar this only happens if generation was cut off by max_tokens
                raise LLMError(f"model output is not valid JSON (truncated?): {content[:200]!r}") from exc
        t = r.get("timings", {})
        return Completion(content=content, data=data, prompt_tokens=int(t.get("prompt_n", 0)),
                          cached_tokens=int(t.get("cache_n", 0)), gen_tokens=int(t.get("predicted_n", 0)),
                          seconds=(t.get("prompt_ms", 0) + t.get("predicted_ms", 0)) / 1000)
