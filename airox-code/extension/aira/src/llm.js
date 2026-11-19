"use strict";
// AIRA's local model (llama-server). Chat uses the OpenAI-compatible endpoint with streaming;
// inline suggestions use llama.cpp's /infill (fill-in-the-middle). Only loopback hosts are allowed:
// AIROX Code never sends code off the machine.
const http = require("http");

class ModelError extends Error {}

function checkLocal(url) {
  const u = new URL(url);
  if (!["127.0.0.1", "localhost", "[::1]", "::1"].includes(u.hostname)) {
    throw new ModelError(`AIRA only talks to a model on this computer, not ${u.hostname}`);
  }
  return u;
}

function request(baseUrl, path, body, { signal, timeoutMs = 120000, onChunk } = {}) {
  const u = checkLocal(baseUrl);
  const data = Buffer.from(JSON.stringify(body));
  return new Promise((resolve, reject) => {
    const req = http.request({
      hostname: u.hostname.replace(/^\[|\]$/g, ""), port: u.port || 80, path, method: "POST",
      headers: { "Content-Type": "application/json", "Content-Length": data.length },
      timeout: timeoutMs,
    }, (res) => {
      if (res.statusCode !== 200) {
        let err = "";
        res.on("data", (c) => (err += c));
        res.on("end", () => reject(new ModelError(`model server answered ${res.statusCode}: ${err.slice(0, 200)}`)));
        return;
      }
      let all = "";
      res.setEncoding("utf8");
      res.on("data", (c) => { all += c; if (onChunk) onChunk(c); });
      res.on("end", () => resolve(all));
    });
    req.on("timeout", () => req.destroy(new ModelError("the model took too long to answer")));
    req.on("error", (e) => reject(e instanceof ModelError ? e
      : new ModelError(`AIRA's model is not running at ${baseUrl} (${e.code || e.message})`)));
    if (signal) {
      if (signal.aborted) return req.destroy(new ModelError("cancelled"));
      signal.addEventListener("abort", () => req.destroy(new ModelError("cancelled")), { once: true });
    }
    req.end(data);
  });
}

// Parses an OpenAI-style SSE stream incrementally: feed() raw text, get content deltas back.
class SSEParser {
  constructor() { this.buf = ""; this.done = false; }
  feed(text) {
    this.buf += text;
    const out = [];
    let i;
    while ((i = this.buf.indexOf("\n")) >= 0) {
      const line = this.buf.slice(0, i).trim();
      this.buf = this.buf.slice(i + 1);
      if (!line.startsWith("data:")) continue;
      const payload = line.slice(5).trim();
      if (payload === "[DONE]") { this.done = true; continue; }
      try {
        const j = JSON.parse(payload);
        const d = j.choices && j.choices[0] && j.choices[0].delta && j.choices[0].delta.content;
        if (d) out.push(d);
      } catch (_) { /* partial or keep-alive line */ }
    }
    return out;
  }
}

async function chat(baseUrl, messages, { onDelta, signal, maxTokens = 1024, temperature = 0.2 } = {}) {
  const parser = new SSEParser();
  let text = "";
  await request(baseUrl, "/v1/chat/completions",
    { messages, stream: true, temperature, max_tokens: maxTokens, cache_prompt: true },
    { signal, onChunk: (c) => { for (const d of parser.feed(c)) { text += d; if (onDelta) onDelta(d, text); } } });
  return text;
}

async function infill(baseUrl, { prefix, suffix, extra = [], nPredict = 48, signal, timeoutMs = 8000 }) {
  const raw = await request(baseUrl, "/infill", {
    input_prefix: prefix, input_suffix: suffix,
    input_extra: extra.map((e) => ({ filename: e.filename, text: e.text })),
    n_predict: nPredict, temperature: 0.1, top_k: 40, cache_prompt: true, stream: false,
  }, { signal, timeoutMs });
  const j = JSON.parse(raw);
  return typeof j.content === "string" ? j.content : "";
}

async function health(baseUrl) {
  const u = checkLocal(baseUrl);
  return new Promise((resolve) => {
    const req = http.get({ hostname: u.hostname, port: u.port || 80, path: "/health", timeout: 2000 }, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on("error", () => resolve(false));
    req.on("timeout", () => { req.destroy(); resolve(false); });
  });
}

module.exports = { chat, infill, health, SSEParser, ModelError, checkLocal };
