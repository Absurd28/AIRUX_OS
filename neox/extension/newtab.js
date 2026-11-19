"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function tick() {
  const d = new Date();
  $("time").textContent = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const h = d.getHours();
  $("hello").textContent = (h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening") + " · " +
    d.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
}
tick(); setInterval(tick, 10000);

// an address goes straight there; anything else is a private search
$("search").addEventListener("submit", (e) => {
  const v = $("q").value.trim();
  if (/^(https?:\/\/)?[\w-]+(\.[\w-]+)+(:\d+)?(\/\S*)?$/.test(v) && !/\s/.test(v)) {
    e.preventDefault();
    location.href = /^https?:\/\//.test(v) ? v : "https://" + v;
  }
});

$("ask").addEventListener("submit", async (e) => {
  e.preventDefault();
  const v = $("a").value.trim();
  if (!v) return;
  await chrome.storage.session.set({ handoff: v });
  chrome.runtime.sendMessage({ type: "open_panel" });
  $("a").value = "";
});

chrome.topSites.get((sites) => {
  $("sites").innerHTML = sites.slice(0, 8).map((s) => {
    const host = new URL(s.url).host.replace(/^www\./, "");
    return `<a href="${esc(s.url)}" title="${esc(s.title)}"><span class="ico">${esc(host[0] || "•").toUpperCase()}</span><span>${esc(host)}</span></a>`;
  }).join("");
});

chrome.runtime.sendMessage({ type: "status" }, (r) => {
  $("aira-state").textContent = r && r.aira ? "🤖 AIRA is ready (Ctrl+Shift+A)" : "🤖 AIRA is offline";
});
