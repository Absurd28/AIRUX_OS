(function () {
  const vscode = acquireVsCodeApi();
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  document.querySelectorAll("button[data-a]").forEach((b) =>
    b.addEventListener("click", () => vscode.postMessage({ type: "action", action: b.dataset.a })));
  document.getElementById("ask").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = document.getElementById("q");
    if (q.value.trim()) { vscode.postMessage({ type: "ask", text: q.value.trim() }); q.value = ""; }
  });
  window.addEventListener("message", (ev) => {
    if (ev.data.type !== "recent") return;
    const box = document.getElementById("recent");
    box.innerHTML = ev.data.items.length
      ? ev.data.items.map((r) => `<button class="recent" data-uri="${esc(r.uri)}">📂 ${esc(r.name)}<small>${esc(r.where)}</small></button>`).join("")
      : '<p class="muted">No recent folders yet. Open one to start.</p>';
    box.querySelectorAll("button.recent").forEach((b) =>
      b.addEventListener("click", () => vscode.postMessage({ type: "open", uri: b.dataset.uri })));
  });
  vscode.postMessage({ type: "ready" });
})();
