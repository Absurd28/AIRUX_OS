"use strict";
// AIRA Home: AIROX Code's own start page (replaces VS Code's Welcome page).
const vscode = require("vscode");
const path = require("path");

const ACTIONS = {
  newFile: "workbench.action.files.newUntitledFile",
  openFolder: "workbench.action.files.openFolder",
  openFile: "workbench.action.files.openFile",
  clone: "git.clone",
  terminal: "workbench.action.terminal.new",
  theme: "workbench.action.selectTheme",
  settings: "workbench.action.openSettings",
  shortcuts: "workbench.action.openGlobalKeybindings",
  extensions: "workbench.view.extensions",
};

async function recentFolders() {
  try {
    const r = await vscode.commands.executeCommand("_workbench.getRecentlyOpened");
    return (r && r.workspaces || []).slice(0, 8).map((w) => {
      const uri = w.folderUri || (w.workspace && w.workspace.configPath);
      return uri ? { uri: uri.toString(), name: path.basename(uri.fsPath || uri.path), where: uri.fsPath || uri.path } : null;
    }).filter(Boolean);
  } catch (_) { return []; }
}

let panel = null;

async function show(ctx, { modelUp, onAsk }) {
  if (panel) { panel.reveal(); return panel; }
  const media = vscode.Uri.joinPath(ctx.extensionUri, "media");
  panel = vscode.window.createWebviewPanel("aira.home", "AIRA Home", vscode.ViewColumn.One,
    { enableScripts: true, localResourceRoots: [media] });
  panel.iconPath = vscode.Uri.joinPath(media, "aira.png");
  const w = panel.webview;
  const nonce = Math.random().toString(36).slice(2) + Date.now().toString(36);
  const css = w.asWebviewUri(vscode.Uri.joinPath(media, "home.css"));
  const js = w.asWebviewUri(vscode.Uri.joinPath(media, "home.js"));
  const logo = w.asWebviewUri(vscode.Uri.joinPath(media, "airox-code.svg"));
  const hour = new Date().getHours();
  const hello = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  w.html = `<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src ${w.cspSource}; style-src ${w.cspSource}; script-src 'nonce-${nonce}';">
<link rel="stylesheet" href="${css}"></head><body>
<main>
  <section class="hero">
    <img class="logo" src="${logo}" alt="">
    <div><h1>AIROX Code</h1><p class="sub">${hello}. What are we building today?</p></div>
    <span class="model ${modelUp ? "up" : "down"}">${modelUp ? "AIRA is ready" : "AIRA's model is off"}</span>
  </section>
  <form id="ask"><input id="q" placeholder="Ask AIRA anything about your code…" autocomplete="off"><button>Ask AIRA</button></form>
  <section class="grid">
    <div class="card"><h2>Start</h2>
      <button data-a="newFile">📄 New file</button>
      <button data-a="openFolder">📁 Open folder</button>
      <button data-a="openFile">🗂️ Open file</button>
      <button data-a="clone">🔗 Clone a Git repository</button>
      <button data-a="terminal">⌨️ New terminal</button>
    </div>
    <div class="card"><h2>Recent</h2><div id="recent"><p class="muted">Loading…</p></div></div>
    <div class="card"><h2>AIRA can</h2>
      <p>💬 Answer questions about your code, citing the lines she used.</p>
      <p>✨ Suggest code as you type (Tab accepts).</p>
      <p>🛠️ Fix, refactor, document or test a selection, applied only after you review it (Ctrl+I).</p>
      <p>📝 Write commit messages and explain errors.</p>
    </div>
    <div class="card"><h2>Make it yours</h2>
      <button data-a="theme">🎨 Theme (Midnight Glass, light or dark)</button>
      <button data-a="settings">⚙️ Settings</button>
      <button data-a="shortcuts">⌨️ Keyboard shortcuts</button>
      <button data-a="extensions">🧩 Extensions</button>
    </div>
  </section>
</main>
<script nonce="${nonce}" src="${js}"></script></body></html>`;
  w.onDidReceiveMessage(async (m) => {
    if (m.type === "action" && ACTIONS[m.action]) return vscode.commands.executeCommand(ACTIONS[m.action]);
    if (m.type === "open" && m.uri) return vscode.commands.executeCommand("vscode.openFolder", vscode.Uri.parse(m.uri));
    if (m.type === "ask" && m.text) return onAsk(m.text);
    if (m.type === "ready") w.postMessage({ type: "recent", items: await recentFolders() });
  });
  panel.onDidDispose(() => { panel = null; });
  return panel;
}

module.exports = { show, ACTIONS };
