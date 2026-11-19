"use strict";
// AIRA in AIROX Code: chat, inline suggestions, reviewed edits, commit messages, error explanations.
// Everything runs on AIRA's local model (llama-server on this computer).
const vscode = require("vscode");
const fs = require("fs");
const path = require("path");
const llm = require("./src/llm");
const prompts = require("./src/prompts");
const grounding = require("./src/grounding");
const textops = require("./src/textops");

const cfg = () => vscode.workspace.getConfiguration("aira");
const serverUrl = () => cfg().get("serverUrl", "http://127.0.0.1:8080");
const NOT_RUNNING = "AIRA's model isn't running. Start it with `sudo systemctl start aira-llm`, then try again.";

// ------------------------------------------------------------------ context for chat answers
function rel(uri) { return vscode.workspace.asRelativePath(uri, false); }

function clip(text, firstLine, max) {
  if (text.length <= max) return { text, firstLine };
  const lines = text.split("\n");
  const out = [];
  let n = 0;
  for (const l of lines) { if (n + l.length + 1 > max) break; out.push(l); n += l.length + 1; }
  return { text: out.join("\n"), firstLine };
}

async function gatherContext(question, maxChars) {
  const files = [];
  const seen = new Set();
  let budget = maxChars;
  const add = (p, text, firstLine = 1) => {
    if (seen.has(p) || budget < 200) return;
    const c = clip(text, firstLine, Math.min(budget, Math.floor(maxChars * 0.6)));
    files.push({ path: p, text: c.text, firstLine: c.firstLine });
    seen.add(p);
    budget -= c.text.length;
  };
  const ed = vscode.window.activeTextEditor;
  if (ed && ed.document.uri.scheme === "file") {
    const doc = ed.document;
    if (!ed.selection.isEmpty) {
      add(rel(doc.uri), doc.getText(ed.selection), ed.selection.start.line + 1);
    } else {
      const cur = ed.selection.active.line;
      const from = Math.max(0, cur - 150);
      const to = Math.min(doc.lineCount - 1, cur + 150);
      add(rel(doc.uri), doc.getText(new vscode.Range(from, 0, to, doc.lineAt(to).text.length)), from + 1);
    }
  }
  // @path mentions, then files that contain the identifiers the question names
  const mentions = (question.match(/@([\w./-]+)/g) || []).map((m) => m.slice(1));
  for (const m of mentions) {
    const found = await vscode.workspace.findFiles(`**/${m}`, "**/{node_modules,.git,.venv,venv,dist,build}/**", 1);
    if (found[0]) add(rel(found[0]), fs.readFileSync(found[0].fsPath, "utf8"));
  }
  const terms = prompts.searchTerms(question.replace(/@[\w./-]+/g, ""));
  if (terms.length && budget > 1000 && vscode.workspace.workspaceFolders) {
    const uris = await vscode.workspace.findFiles("**/*.{py,js,ts,jsx,tsx,java,c,h,cpp,hpp,rs,go,rb,php,cs,kt,swift,sh,md,json,toml,yaml,yml}",
      "**/{node_modules,.git,.venv,venv,dist,build,__pycache__}/**", 400);
    for (const u of uris) {
      if (budget < 1000 || files.length >= 6) break;
      let text;
      try { const st = fs.statSync(u.fsPath); if (st.size > 200000) continue; text = fs.readFileSync(u.fsPath, "utf8"); }
      catch (_) { continue; }
      const lines = text.split("\n");
      const hit = lines.findIndex((l) => terms.some((t) => l.includes(t)));
      if (hit < 0) continue;
      const from = Math.max(0, hit - 40);
      add(rel(u), lines.slice(from, hit + 80).join("\n"), from + 1);
    }
  }
  return files;
}

// One grounded answer: context from the editor and workspace, the model's reply, and the citation check.
async function answer(question, { history = [], onDelta, signal } = {}) {
  if (!(await llm.health(serverUrl()))) throw new llm.ModelError(NOT_RUNNING);
  const files = await gatherContext(question, cfg().get("context.maxChars", 12000));
  const text = await llm.chat(serverUrl(), prompts.chatMessages(question, files, history), { signal, onDelta });
  return { text, files, check: grounding.checkAnswer(text, files) };
}

// ------------------------------------------------------------------ chat view
class ChatView {
  constructor(ctx) { this.ctx = ctx; this.view = null; this.history = []; this.abort = null; this.pending = []; }

  resolveWebviewView(view) {
    this.view = view;
    const media = vscode.Uri.joinPath(this.ctx.extensionUri, "media");
    view.webview.options = { enableScripts: true, localResourceRoots: [media] };
    const nonce = Math.random().toString(36).slice(2) + Date.now().toString(36);
    const css = view.webview.asWebviewUri(vscode.Uri.joinPath(media, "chat.css"));
    const js = view.webview.asWebviewUri(vscode.Uri.joinPath(media, "chat.js"));
    view.webview.html = `<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${view.webview.cspSource}; script-src 'nonce-${nonce}';">
<link rel="stylesheet" href="${css}"></head><body>
<div id="log"><div class="hello"><b>Hi, I'm AIRA.</b> Ask about your code, or select code and right-click → AIRA.
I answer from your files and cite them; if I can't find it, I'll say I'm not sure. Everything stays on this computer.</div></div>
<form id="f"><textarea id="q" rows="3" placeholder="Ask AIRA about your code (use @file.py to add a file)"></textarea>
<button type="submit">Ask</button></form>
<script nonce="${nonce}" src="${js}"></script></body></html>`;
    view.webview.onDidReceiveMessage((m) => this.onMessage(m));
    for (const q of this.pending.splice(0)) this.ask(q);
  }

  async reveal() {
    await vscode.commands.executeCommand("aira.chat.focus");
  }

  async onMessage(m) {
    if (m.type === "ask") return this.ask(m.text);
    if (m.type === "stop" && this.abort) return this.abort.abort();
    if (m.type === "open") {
      const uris = await vscode.workspace.findFiles(`**/${m.path}`, "**/node_modules/**", 1);
      if (!uris[0]) return;
      const doc = await vscode.workspace.openTextDocument(uris[0]);
      const line = Math.max(0, (m.line || 1) - 1);
      await vscode.window.showTextDocument(doc, { selection: new vscode.Range(line, 0, line, 0), preview: true });
    }
    if (m.type === "insert") {
      const ed = vscode.window.activeTextEditor;
      if (!ed) return vscode.window.showInformationMessage("Open a file first, then insert.");
      await ed.edit((e) => e.replace(ed.selection, m.code));
    }
    if (m.type === "copy") await vscode.env.clipboard.writeText(m.code);
  }

  newChat() {
    this.history = [];
    if (this.view) this.view.webview.postMessage({ type: "clear" });
  }

  async ask(question) {
    if (!this.view) { this.pending.push(question); await this.reveal(); return; }
    const post = (msg) => this.view.webview.postMessage(msg);
    post({ type: "user", text: question });
    post({ type: "start", sources: [] });
    this.abort = new AbortController();
    try {
      const r = await answer(question, { history: this.history, signal: this.abort.signal,
                                         onDelta: (d) => post({ type: "delta", text: d }) });
      post({ type: "done", status: r.check.status, bad: r.check.bad.map((b) => b.raw), sources: r.files.map((f) => f.path) });
      this.history.push({ role: "user", content: question }, { role: "assistant", content: r.text });
    } catch (e) {
      post({ type: "error", text: /not running/.test(e.message) ? NOT_RUNNING : "I couldn't finish that: " + e.message });
    } finally { this.abort = null; }
  }
}

// ------------------------------------------------------------------ reviewed edits
class Proposals {
  constructor() { this.map = new Map(); this.emitter = new vscode.EventEmitter(); this.onDidChange = this.emitter.event; this.current = null; }
  provideTextDocumentContent(uri) { return (this.map.get(uri.query) || {}).text || ""; }
}

async function proposeEdit(proposals, task, instruction) {
  const ed = vscode.window.activeTextEditor;
  if (!ed) return vscode.window.showInformationMessage("Open a file and select the code for AIRA to change.");
  if (!(await llm.health(serverUrl()))) return vscode.window.showErrorMessage(NOT_RUNNING);
  const doc = ed.document;
  const range = ed.selection.isEmpty ? new vscode.Range(0, 0, doc.lineCount, 0) : new vscode.Range(ed.selection.start, ed.selection.end);
  const code = doc.getText(range);
  if (!code.trim()) return vscode.window.showInformationMessage("There is no code there for AIRA to change.");
  const answer = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: `AIRA: ${instruction || task}…`, cancellable: true },
    (_p, token) => {
      const ac = new AbortController();
      token.onCancellationRequested(() => ac.abort());
      return llm.chat(serverUrl(), prompts.editMessages(task, code, doc.languageId, instruction),
        { signal: ac.signal, maxTokens: 2048, temperature: 0.1 }).catch((e) => { throw e; });
    }).then((a) => a, (e) => { vscode.window.showErrorMessage(/not running/.test(e.message) ? NOT_RUNNING : "AIRA couldn't finish: " + e.message); return null; });
  if (answer === null) return;
  const proposed = textops.extractCode(answer);
  if (proposed === null) return vscode.window.showWarningMessage("AIRA's answer was cut off, so nothing was changed. Try a smaller selection.");

  let target, original, newFull, replaceRange, isNewFile = false;
  if (task === "tests") {
    target = testFileFor(doc.uri);
    isNewFile = !fs.existsSync(target.fsPath);
    original = isNewFile ? "" : fs.readFileSync(target.fsPath, "utf8");
    newFull = isNewFile ? proposed + "\n" : original.replace(/\s*$/, "\n\n") + proposed + "\n";
  } else {
    target = doc.uri;
    original = doc.getText();
    replaceRange = range;
    const before = doc.getText(new vscode.Range(new vscode.Position(0, 0), range.start));
    const after = doc.getText(new vscode.Range(range.end, doc.lineAt(doc.lineCount - 1).range.end));
    newFull = before + textops.matchEnds(code, proposed) + after;
  }
  if (newFull === original) return vscode.window.showInformationMessage("AIRA found nothing to change.");
  const id = String(Date.now());
  proposals.map.set(id, { text: newFull });
  proposals.current = { id, target, original, newFull, isNewFile, version: task === "tests" ? null : doc.version, docUri: doc.uri };
  const right = vscode.Uri.from({ scheme: "aira-proposal", path: target.path, query: id });
  const left = isNewFile ? vscode.Uri.from({ scheme: "aira-proposal", path: target.path + ".empty", query: "empty" }) : target;
  await vscode.commands.executeCommand("setContext", "aira.reviewing", true);
  await vscode.commands.executeCommand("vscode.diff", left, right,
    `AIRA: ${instruction || task} — review (Ctrl+Enter to accept, Esc to reject)`);
  vscode.window.showInformationMessage("Review AIRA's change. Nothing is changed until you accept.", "Accept", "Reject")
    .then((pick) => {
      if (proposals.current && proposals.current.id === id) {
        if (pick === "Accept") return acceptEdit(proposals);
        if (pick === "Reject") return rejectEdit(proposals);
      }
    });
  return proposals.current;
}

function testFileFor(uri) {
  const dir = path.dirname(uri.fsPath);
  const ext = path.extname(uri.fsPath);
  const base = path.basename(uri.fsPath, ext);
  let name;
  if (ext === ".py") name = path.join(dir, `test_${base}.py`);
  else if ([".js", ".ts", ".jsx", ".tsx"].includes(ext)) name = path.join(dir, `${base}.test${ext}`);
  else name = path.join(dir, `${base}_test${ext}`);
  return vscode.Uri.file(name);
}

async function closeReview() {
  await vscode.commands.executeCommand("setContext", "aira.reviewing", false);
  for (const group of vscode.window.tabGroups.all) {
    for (const tab of group.tabs) {
      const inp = tab.input;
      if (inp && inp.modified && inp.modified.scheme === "aira-proposal") await vscode.window.tabGroups.close(tab);
    }
  }
}

async function acceptEdit(proposals) {
  const p = proposals.current;
  if (!p) return;
  proposals.current = null;
  const we = new vscode.WorkspaceEdit();
  if (p.isNewFile) {
    we.createFile(p.target, { ignoreIfExists: false });
    we.insert(p.target, new vscode.Position(0, 0), p.newFull);
  } else {
    const doc = await vscode.workspace.openTextDocument(p.target);
    if (p.version !== null && doc.version !== p.version) {
      await closeReview();
      return vscode.window.showWarningMessage("The file changed after AIRA proposed this, so nothing was applied. Ask again.");
    }
    we.replace(p.target, new vscode.Range(0, 0, doc.lineCount, 0), p.newFull);
  }
  const ok = await vscode.workspace.applyEdit(we);
  await closeReview();
  // read-back: the document must now hold exactly what was accepted
  const after = (await vscode.workspace.openTextDocument(p.target)).getText();
  if (ok && after === p.newFull) {
    await vscode.window.showTextDocument(p.target, { preview: false });
    vscode.window.showInformationMessage("AIRA's change was applied and checked. Save when you're happy (Ctrl+S).");
  } else {
    vscode.window.showErrorMessage("I couldn't verify that the change was applied. Please check the file.");
  }
}

async function rejectEdit(proposals) {
  proposals.current = null;
  await closeReview();
  vscode.window.setStatusBarMessage("AIRA's change was rejected; nothing changed.", 4000);
}

// ------------------------------------------------------------------ inline suggestions
class InlineProvider {
  constructor(status) { this.status = status; this.abort = null; }
  async provideInlineCompletionItems(doc, pos, _context, token) {
    if (!cfg().get("inlineSuggestions.enabled", true) || doc.uri.scheme !== "file") return [];
    const wait = cfg().get("inlineSuggestions.debounceMs", 350);
    await new Promise((r) => setTimeout(r, wait));
    if (token.isCancellationRequested) return [];
    if (this.abort) this.abort.abort();
    const ac = new AbortController();
    this.abort = ac;
    token.onCancellationRequested(() => ac.abort());
    const off = doc.offsetAt(pos);
    const all = doc.getText();
    const prefix = all.slice(Math.max(0, off - 3000), off);
    const suffix = all.slice(off, off + 1200);
    const prefixLine = doc.lineAt(pos.line).text.slice(0, pos.character);
    try {
      this.status.text = "$(loading~spin) AIRA";
      const raw = await llm.infill(serverUrl(), { prefix, suffix, signal: ac.signal, nPredict: 64 });
      this.status.text = "$(sparkle) AIRA";
      const text = textops.cleanInline(raw, { prefixLine, suffix });
      if (!text || token.isCancellationRequested) return [];
      return [new vscode.InlineCompletionItem(text, new vscode.Range(pos, pos))];
    } catch (_) {
      this.status.text = "$(circle-slash) AIRA";
      this.status.tooltip = "AIRA's model is not reachable; inline suggestions are paused.";
      return [];
    }
  }
}

// ------------------------------------------------------------------ git and terminal help
async function commitMessage() {
  const gitExt = vscode.extensions.getExtension("vscode.git");
  if (!gitExt) return vscode.window.showErrorMessage("Git support is not available.");
  const git = (gitExt.isActive ? gitExt.exports : await gitExt.activate()).getAPI(1);
  const repo = git.repositories[0];
  if (!repo) return vscode.window.showInformationMessage("Open a folder that is a git repository first.");
  let diff = await repo.diff(true);
  if (!diff.trim()) diff = await repo.diff(false);
  if (!diff.trim()) return vscode.window.showInformationMessage("There are no changes to describe.");
  if (!(await llm.health(serverUrl()))) return vscode.window.showErrorMessage(NOT_RUNNING);
  repo.inputBox.value = "AIRA is writing…";
  try {
    const msg = await llm.chat(serverUrl(), prompts.commitMessages(diff), { maxTokens: 200, temperature: 0.2 });
    repo.inputBox.value = textops.cleanCommitMessage(msg);
  } catch (e) {
    repo.inputBox.value = "";
    vscode.window.showErrorMessage("AIRA couldn't write the message: " + e.message);
  }
}

async function explainTerminal(chat) {
  const before = await vscode.env.clipboard.readText();
  await vscode.commands.executeCommand("workbench.action.terminal.copySelection");
  const text = (await vscode.env.clipboard.readText()).trim();
  await vscode.env.clipboard.writeText(before);
  if (!text || text === before.trim()) {
    return vscode.window.showInformationMessage("Select the error text in the terminal first, then choose Explain with AIRA.");
  }
  await chat.reveal();
  chat.ask("Explain this terminal output and how to fix it:\n```\n" + text.slice(0, 6000) + "\n```");
}

async function explainProblems(chat) {
  const ed = vscode.window.activeTextEditor;
  if (!ed) return;
  const diags = vscode.languages.getDiagnostics(ed.document.uri);
  if (!diags.length) return vscode.window.showInformationMessage("There are no problems in this file.");
  const list = diags.slice(0, 20).map((d) => `${rel(ed.document.uri)}:${d.range.start.line + 1}: ${d.message}`).join("\n");
  await chat.reveal();
  chat.ask("Explain these problems and how to fix them:\n" + list);
}

// ------------------------------------------------------------------ activation
function activate(ctx) {
  const chat = new ChatView(ctx);
  const proposals = new Proposals();
  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  status.text = "$(sparkle) AIRA";
  status.tooltip = "AIRA inline suggestions (click to turn on or off)";
  status.command = "aira.toggleInline";
  status.show();
  const inline = new InlineProvider(status);
  llm.health(serverUrl()).then((up) => { if (!up) { status.text = "$(circle-slash) AIRA"; status.tooltip = NOT_RUNNING; } });

  const reg = (id, fn) => ctx.subscriptions.push(vscode.commands.registerCommand(id, fn));
  ctx.subscriptions.push(
    status,
    vscode.window.registerWebviewViewProvider("aira.chat", chat, { webviewOptions: { retainContextWhenHidden: true } }),
    vscode.workspace.registerTextDocumentContentProvider("aira-proposal", proposals),
    vscode.languages.registerInlineCompletionItemProvider({ pattern: "**" }, inline),
  );
  reg("aira.ask", async () => {
    await chat.reveal();
    const ed = vscode.window.activeTextEditor;
    if (ed && !ed.selection.isEmpty) chat.ask("Explain this code.");
  });
  reg("aira.newChat", () => chat.newChat());
  reg("aira.explainSelection", async () => { await chat.reveal(); chat.ask("Explain the selected code."); });
  reg("aira.fix", () => proposeEdit(proposals, "fix"));
  reg("aira.refactor", () => proposeEdit(proposals, "refactor"));
  reg("aira.document", () => proposeEdit(proposals, "document"));
  reg("aira.tests", () => proposeEdit(proposals, "tests"));
  reg("aira.editWithInstruction", async () => {
    const what = await vscode.window.showInputBox({ prompt: "What should AIRA change?", placeHolder: "e.g. handle the empty list case" });
    if (what && what.trim()) await proposeEdit(proposals, "custom", what.trim());
  });
  reg("aira.acceptEdit", () => acceptEdit(proposals));
  reg("aira.rejectEdit", () => rejectEdit(proposals));
  reg("aira.commitMessage", commitMessage);
  reg("aira.explainTerminal", () => explainTerminal(chat));
  reg("aira.explainProblems", () => explainProblems(chat));
  reg("aira.toggleInline", async () => {
    const on = !cfg().get("inlineSuggestions.enabled", true);
    await cfg().update("inlineSuggestions.enabled", on, vscode.ConfigurationTarget.Global);
    status.text = on ? "$(sparkle) AIRA" : "$(sparkle) AIRA (off)";
  });
  return { chat, proposals, inline, answer, proposeEdit, acceptEdit, rejectEdit, commitMessage };   // for the integration tests
}

function deactivate() {}

module.exports = { activate, deactivate };
