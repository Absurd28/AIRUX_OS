"use strict";
const port = chrome.runtime.connect({ name: "panel" });
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
let n = 0;
const pending = new Map();

function md(text) {
  let t = esc(text);
  t = t.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>").replace(/`([^`\n]+)`/g, "<code>$1</code>");
  t = t.replace(/^\s*[-*•]\s+(.*)$/gm, "<li>$1</li>").replace(/(<li>.*<\/li>\n?)+/g, (m) => "<ul>" + m + "</ul>");
  return t.replace(/\n/g, "<br>");
}
function bubble(log, cls, html) {
  const d = document.createElement("div");
  d.className = "msg " + cls;
  d.innerHTML = html;
  $(log).appendChild(d);
  $(log).scrollTop = $(log).scrollHeight;
  return d;
}

function ask(question) {
  const id = "p" + (++n);
  bubble("page-log", "you", esc(question));
  pending.set(id, bubble("page-log", "aira", '<span class="typing">Reading the page…</span>'));
  port.postMessage({ type: "ask_page", id, question });
}
function command(text) {
  const id = "c" + (++n);
  bubble("aira-log", "you", esc(text));
  pending.set(id, bubble("aira-log", "aira", '<span class="typing">AIRA is working…</span>'));
  port.postMessage({ type: "aira_command", id, text });
}

port.onMessage.addListener((m) => {
  if (m.type === "status") {
    $("status").textContent = m.aira ? "ready" : "offline";
    $("status").className = "pill " + (m.aira ? "ok" : "off");
    return;
  }
  const el = pending.get(m.id);
  if (m.type === "answer" && el) {
    const labels = { verified: "✓ quotes found on the page", unverified: "⚠ unverified", "not sure": "not sure" };
    let foot = `<div class="tag ${m.status.replace(" ", "-")}">${labels[m.status] || m.status}</div>`;
    if (m.unverified && m.unverified.length) foot += `<div class="warn">Not found on the page: ${m.unverified.map((q) => "“" + esc(q) + "”").join(", ")}</div>`;
    el.innerHTML = md(m.text) + foot;
    pending.delete(m.id);
  } else if (m.type === "aira_event" && el) {
    if (m.event === "aira") { el.innerHTML = md(m.text); pending.delete(m.id); }
    else if (m.event === "confirm") el.insertAdjacentHTML("beforeend", `<div class="warn">AIRA is asking for your confirmation in her panel: ${esc(m.text)}</div>`);
  } else if (m.type === "error") {
    if (el) { el.className = "msg error"; el.innerHTML = esc(m.text); pending.delete(m.id); }
    else bubble($("page").classList.contains("on") ? "page-log" : "aira-log", "error", esc(m.text));
  }
});

document.querySelectorAll(".quick button").forEach((b) => b.addEventListener("click", () => ask(b.dataset.q)));
for (const [form, input, fn] of [["page-form", "page-q", ask], ["aira-form", "aira-q", command]]) {
  $(form).addEventListener("submit", (e) => { e.preventDefault(); const v = $(input).value.trim(); if (v) { $(input).value = ""; fn(v); } });
  $(input).addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $(form).requestSubmit(); } });
}
for (const [btn, sec] of [["tab-page", "page"], ["tab-aira", "aira"]]) {
  $(btn).addEventListener("click", () => {
    document.querySelectorAll("nav button, section").forEach((x) => x.classList.remove("on"));
    $(btn).classList.add("on"); $(sec).classList.add("on");
  });
}
// the new tab page can hand a question over
chrome.storage.session.get("handoff").then(({ handoff }) => {
  if (handoff) { chrome.storage.session.remove("handoff"); $("tab-aira").click(); command(handoff); }
}).catch(() => {});
