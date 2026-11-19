"use strict";
// Runs inside AIROX Code (VS Code's extension test API) against AIRA's real local model.
// The workspace is a throwaway git project made by tests/airox_code_check.py, where sub() in calc.py
// has a bug (returns a + b). Results go to $AIRA_IT_RESULTS as JSON; the check script judges them.
const vscode = require("vscode");
const fs = require("fs");
const path = require("path");

const results = {};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const save = () => fs.writeFileSync(process.env.AIRA_IT_RESULTS, JSON.stringify(results, null, 2));

async function step(name, fn, limitMs = 240000) {
  const t0 = Date.now();
  console.log(`[aira-it] ${name}: start`);
  let timer;
  const limit = new Promise((_, rej) => { timer = setTimeout(() => rej(new Error(`timed out after ${limitMs / 1000}s`)), limitMs); });
  try {
    results[name] = { ok: true, ...(await Promise.race([fn(), limit])), seconds: Math.round((Date.now() - t0) / 100) / 10 };
  } catch (e) {
    results[name] = { ok: false, error: String(e && e.stack || e), seconds: Math.round((Date.now() - t0) / 100) / 10 };
  } finally { clearTimeout(timer); }
  console.log(`[aira-it] ${name}: ${JSON.stringify(results[name]).slice(0, 300)}`);
  save();
}

async function run() {
  const root = vscode.workspace.workspaceFolders[0].uri.fsPath;
  const ext = vscode.extensions.getExtension("airux.aira");
  console.log("[aira-it] activating", ext ? ext.id : "MISSING");
  const api = await ext.activate();
  results.product = vscode.env.appName;
  save();
  const calcUri = vscode.Uri.file(path.join(root, "calc.py"));
  const doc = await vscode.workspace.openTextDocument(calcUri);
  const ed = await vscode.window.showTextDocument(doc);

  await step("inline", async () => {
    // cursor in the empty body of mul()
    const line = doc.getText().split("\n").findIndex((l) => l.startsWith("def mul("));
    const pos = new vscode.Position(line + 1, 4);
    ed.selection = new vscode.Selection(pos, pos);
    const cts = new vscode.CancellationTokenSource();
    const items = await api.inline.provideInlineCompletionItems(doc, pos, {}, cts.token);
    const text = items[0] ? String(items[0].insertText) : "";
    return { suggestion: text, pass: /return/.test(text) && /\*/.test(text) };
  });

  await step("chat", async () => {
    const line = doc.getText().split("\n").findIndex((l) => l.startsWith("def sub("));
    ed.selection = new vscode.Selection(line, 0, line + 2, 0);
    let r;
    for (let attempt = 1; attempt <= 2; attempt++) {
      r = await api.answer("Why does sub in calc.py give the wrong result? Cite the line.");
      if (r.check.status === "verified") break;
    }
    return { answer: r.text, status: r.check.status, read: r.files.map((f) => f.path),
             pass: r.check.status === "verified" && /\+/.test(r.text) };
  });

  await step("edit_review", async () => {
    const lines = doc.getText().split("\n");
    const s = lines.findIndex((l) => l.startsWith("def sub("));
    ed.selection = new vscode.Selection(s, 0, s + 2, 0);
    const before = doc.getText();
    const proposal = await api.proposeEdit(api.proposals, "fix");
    await sleep(500);
    const unchangedUntilAccepted = doc.getText() === before;
    if (!proposal) return { pass: false, reason: "no proposal" };
    await api.acceptEdit(api.proposals);
    const after = doc.getText();
    const subBody = after.split("\n").slice(s, s + 2).join("\n");
    await doc.save();
    return { proposal_made: true, unchanged_until_accepted: unchangedUntilAccepted, new_sub: subBody,
             pass: unchangedUntilAccepted && /a\s*-\s*b/.test(subBody) && after !== before };
  });

  await step("commit_message", async () => {
    const git = vscode.extensions.getExtension("vscode.git").exports.getAPI(1);
    for (let i = 0; i < 40 && !git.repositories.length; i++) await sleep(250);
    const repo = git.repositories[0];
    await repo.status();
    await api.commitMessage();
    const msg = repo.inputBox.value;
    return { message: msg, pass: !!msg.trim() && msg.split("\n")[0].length <= 72 && !/AIRA is writing/.test(msg) };
  });

  save();
  console.log("[aira-it] done");
}

module.exports = { run };
