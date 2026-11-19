// AIRA chat panel (runs inside the webview). Renders a small, safe subset of Markdown:
// code blocks (with Insert / Copy), inline code, bold, and [path:line] citations as links.
(function () {
  const vscode = acquireVsCodeApi();
  const log = document.getElementById("log");
  const form = document.getElementById("f");
  const q = document.getElementById("q");
  let current = null;
  let raw = "";

  const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  function render(md) {
    const parts = md.split(/```/);
    let html = "";
    parts.forEach((part, i) => {
      if (i % 2 === 1) {
        const nl = part.indexOf("\n");
        const lang = nl >= 0 ? part.slice(0, nl).trim() : "";
        const code = nl >= 0 ? part.slice(nl + 1).replace(/\n$/, "") : part;
        html += `<div class="code"><div class="bar"><span>${esc(lang)}</span>` +
                `<button data-act="insert">Insert</button><button data-act="copy">Copy</button></div>` +
                `<pre><code>${esc(code)}</code></pre></div>`;
      } else {
        let t = esc(part);
        t = t.replace(/`([^`\n]+)`/g, "<code>$1</code>");
        t = t.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
        t = t.replace(/\[([^\]\s:]+\.[A-Za-z0-9]+):(\d+)(?:-(\d+))?\]/g,
          (m, p, a, b) => `<a href="#" class="ref" data-path="${p}" data-line="${a}">${p}:${a}${b ? "-" + b : ""}</a>`);
        html += t.replace(/\n/g, "<br>");
      }
    });
    return html;
  }

  function bubble(cls, html) {
    const d = document.createElement("div");
    d.className = "msg " + cls;
    d.innerHTML = html;
    log.appendChild(d);
    log.scrollTop = log.scrollHeight;
    return d;
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = q.value.trim();
    if (!text) return;
    q.value = "";
    vscode.postMessage({ type: "ask", text });
  });
  q.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });

  log.addEventListener("click", (e) => {
    const a = e.target.closest("a.ref");
    if (a) { e.preventDefault(); vscode.postMessage({ type: "open", path: a.dataset.path, line: +a.dataset.line }); return; }
    const b = e.target.closest("button[data-act]");
    if (b) {
      const code = b.closest(".code").querySelector("code").textContent;
      vscode.postMessage({ type: b.dataset.act, code });
    }
  });

  window.addEventListener("message", (ev) => {
    const m = ev.data;
    if (m.type === "user") bubble("user", esc(m.text).replace(/\n/g, "<br>"));
    else if (m.type === "start") {
      raw = "";
      current = bubble("aira", '<span class="typing">AIRA is thinking…</span>');
    } else if (m.type === "delta" && current) {
      raw += m.text;
      current.innerHTML = render(raw);
      log.scrollTop = log.scrollHeight;
    } else if (m.type === "done" && current) {
      const labels = { "verified": "✓ cites your code", "unverified": "unverified", "not sure": "not sure", "general": "general knowledge" };
      let foot = `<div class="tag ${m.status.replace(" ", "-")}">${labels[m.status] || m.status}</div>`;
      if (m.bad && m.bad.length) foot += `<div class="warn">Couldn't verify: ${esc(m.bad.join(", "))}</div>`;
      if (m.sources && m.sources.length) foot += `<div class="src">Read: ${esc(m.sources.join(", "))}</div>`;
      current.insertAdjacentHTML("beforeend", foot);
      current = null;
    } else if (m.type === "error") {
      if (current) { current.remove(); current = null; }
      bubble("error", render(m.text));
    } else if (m.type === "clear") {
      log.querySelectorAll(".msg").forEach((n) => n.remove());
    }
  });
})();
