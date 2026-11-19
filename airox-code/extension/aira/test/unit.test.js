"use strict";
// Unit tests for AIRA in AIROX Code. Run with AIROX Code's own runtime:
//   ELECTRON_RUN_AS_NODE=1 /usr/share/airox-code/codium --test test/
const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const llm = require("../src/llm");
const prompts = require("../src/prompts");
const grounding = require("../src/grounding");
const textops = require("../src/textops");

const FILES = [{ path: "src/calc.py", firstLine: 1, text: "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a + b" }];

test("context block numbers lines so answers can cite them", () => {
  const c = prompts.contextBlock(FILES);
  assert.match(c, /--- src\/calc\.py \(lines 1-5\)/);
  assert.match(c, /5\|     return a \+ b/);
  const m = prompts.chatMessages("why is sub wrong?", FILES);
  assert.equal(m[0].role, "system");
  assert.match(m[0].content, /I'm not sure/);
  assert.equal(m.at(-1).content, "why is sub wrong?");
});

test("citations must point at lines that were in the context", () => {
  const ok = grounding.checkAnswer("`sub` adds instead of subtracting [src/calc.py:4-5].", FILES);
  assert.equal(ok.status, "verified");
  assert.equal(ok.good.length, 1);
  const badLine = grounding.checkAnswer("See [src/calc.py:40].", FILES);
  assert.equal(badLine.status, "unverified");
  const badFile = grounding.checkAnswer("See [src/other.py:2].", FILES);
  assert.equal(badFile.status, "unverified");
  assert.equal(grounding.checkAnswer("It probably works.", FILES).status, "unverified");
  assert.equal(grounding.checkAnswer("I'm not sure: open the tests file.", FILES).status, "not sure");
  assert.equal(grounding.checkAnswer("A list is mutable.", []).status, "general");
  // a bare file name matches its full path in the context
  assert.equal(grounding.checkAnswer("[calc.py:2]", FILES).status, "verified");
});

test("edit answers: the single code block is extracted, cut-off answers are refused", () => {
  assert.equal(textops.extractCode("```python\ndef sub(a, b):\n    return a - b\n```"), "def sub(a, b):\n    return a - b");
  assert.equal(textops.extractCode("```python\ndef sub(a, b):\n    return a"), null);
  assert.equal(textops.matchEnds("x\n", "y"), "y\n");
  assert.equal(textops.matchEnds("x", "y\n\n"), "y");
});

test("inline suggestions are cleaned before they are shown", () => {
  assert.equal(textops.cleanInline("return a + b\n\n", { prefixLine: "    " }), "return a + b");
  // mid-line: only the rest of this line
  assert.equal(textops.cleanInline("a, b)\n    pass", { prefixLine: "add(", suffix: ")\nnext()" }), "a, b)");
  // does not repeat the text after the cursor
  assert.equal(textops.cleanInline("total = x\nprint(total)", { suffix: "\nprint(total)\n" }), "total = x");
  assert.equal(textops.cleanInline("   \n", {}), "");
  assert.equal(textops.cleanInline(Array(20).fill("x = 1").join("\n"), {}).split("\n").length, 8);
});

test("commit messages are cleaned and the summary is at most 72 characters", () => {
  const m = textops.cleanCommitMessage("```\n" + "Fix the subtraction bug in calc ".repeat(4) + "\n\n- sub returned a + b\n```");
  assert.ok(m.split("\n")[0].length <= 72);
  assert.ok(!m.includes("```"));
  assert.match(m, /- sub returned a \+ b/);
  // seen with the real model: bullets straight under the summary get the blank line git expects
  assert.equal(textops.cleanCommitMessage("Fix subtraction in calc.py\n- sub returns a - b"),
    "Fix subtraction in calc.py\n\n- sub returns a - b");
  assert.equal(textops.cleanCommitMessage("Fix subtraction in calc.py"), "Fix subtraction in calc.py");
});

test("search terms keep identifiers and drop filler words", () => {
  assert.deepEqual(prompts.searchTerms("Where is parse_config used in the loader?"), ["parse_config", "loader?"].map((s) => s.replace("?", "")));
});

test("SSE parser yields content deltas and ignores partial lines", () => {
  const p = new llm.SSEParser();
  assert.deepEqual(p.feed('data: {"choices":[{"delta":{"content":"Hel"}}]}\n\ndata: {"choi'), ["Hel"]);
  assert.deepEqual(p.feed('ces":[{"delta":{"content":"lo"}}]}\n\ndata: [DONE]\n\n'), ["lo"]);
  assert.equal(p.done, true);
});

test("only a model on this computer is allowed", () => {
  assert.throws(() => llm.checkLocal("http://example.com:8080"), llm.ModelError);
  assert.doesNotThrow(() => llm.checkLocal("http://127.0.0.1:8080"));
});

function mockServer(handler) {
  return new Promise((resolve) => {
    const s = http.createServer(handler);
    s.listen(0, "127.0.0.1", () => resolve(s));
  });
}

test("chat streams from the model and infill returns the middle", async () => {
  const seen = [];
  const s = await mockServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      seen.push([req.url, JSON.parse(body)]);
      if (req.url === "/v1/chat/completions") {
        res.writeHead(200, { "Content-Type": "text/event-stream" });
        res.write('data: {"choices":[{"delta":{"content":"Hi "}}]}\n\n');
        res.end('data: {"choices":[{"delta":{"content":"boss"}}]}\n\ndata: [DONE]\n\n');
      } else if (req.url === "/infill") {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ content: "return a + b" }));
      } else { res.writeHead(404); res.end(); }
    });
  });
  const url = `http://127.0.0.1:${s.address().port}`;
  const deltas = [];
  const text = await llm.chat(url, [{ role: "user", content: "hi" }], { onDelta: (d) => deltas.push(d) });
  assert.equal(text, "Hi boss");
  assert.deepEqual(deltas, ["Hi ", "boss"]);
  assert.equal(seen[0][1].stream, true);
  const mid = await llm.infill(url, { prefix: "def add(a, b):\n    ", suffix: "\n" });
  assert.equal(mid, "return a + b");
  assert.equal(seen[1][1].input_prefix, "def add(a, b):\n    ");
  s.close();
});

test("a stopped model is reported plainly", async () => {
  const s = await mockServer(() => {});
  const port = s.address().port;
  s.close();
  await assert.rejects(llm.chat(`http://127.0.0.1:${port}`, []), /not running/);
});
