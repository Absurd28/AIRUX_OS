"use strict";
// the link to NEOX's background; made again if Chromium restarts the background worker
let port = null;
function connectPanel() {
  port = chrome.runtime.connect({ name: "panel" });
  port.onMessage.addListener(onPanelMessage);
  port.onDisconnect.addListener(() => {
    for (const w of vpnWait.values()) w({ ok: false, error: "NEOX's background restarted; try again." });
    vpnWait.clear();
    setTimeout(connectPanel, 500);
  });
}
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

function onPanelMessage(m) {
  if (m.type === "vpn_result") { const w = vpnWait.get(m.id); if (w) { vpnWait.delete(m.id); w(m); } return; }
  if (m.type === "vpn_state") { showVpn(m.state); return; }
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
}

document.querySelectorAll(".quick button").forEach((b) => b.addEventListener("click", () => ask(b.dataset.q)));
for (const [form, input, fn] of [["page-form", "page-q", ask], ["aira-form", "aira-q", command]]) {
  $(form).addEventListener("submit", (e) => { e.preventDefault(); const v = $(input).value.trim(); if (v) { $(input).value = ""; fn(v); } });
  $(input).addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $(form).requestSubmit(); } });
}
for (const [btn, sec] of [["tab-page", "page"], ["tab-aira", "aira"], ["tab-vpn", "vpn"]]) {
  $(btn).addEventListener("click", () => {
    document.querySelectorAll("nav button, section").forEach((x) => x.classList.remove("on"));
    $(btn).classList.add("on"); $(sec).classList.add("on");
    if (sec === "vpn") openVpn();
  });
}
// the new tab page can hand a question over
chrome.storage.session.get("handoff").then(({ handoff }) => {
  if (handoff) { chrome.storage.session.remove("handoff"); $("tab-aira").click(); command(handoff); }
}).catch(() => {});

// ---------------------------------------------------------------- VPN tab
// The service (neox-vpnd) does the work; NEOX's background sets the browser's proxy from its status.
const vpnWait = new Map();
const NAMES = { riseup: "Riseup", tor: "Tor", wireguard: "WireGuard" };
const NOTES = {
  riseup: "RiseupVPN is free and needs no account. Volunteers at riseup.net run it on donations; if it helps you, consider giving them something.",
  tor: "Tor sends your traffic through three volunteer relays: the most private choice, but slower, and some sites block it. Connecting can take a minute.",
  wireguard: "Use configs from a VPN you signed up to (Proton VPN's free plan, for example). Files that run commands (PostUp/PostDown) are refused.",
};
const STATES = { up: "on", connecting: "connecting…", hopping: "changing place…", retrying: "retrying…", reconnecting: "reconnecting…" };
let backend = "riseup", providers = null, vst = null, exitIp = null, nextAt = 0, busy = false, vpnPoll = null;
const region = (() => {
  let d = null;
  try { d = new Intl.DisplayNames(["en"], { type: "region" }); } catch (_) {}
  return (cc) => { if (!cc) return ""; try { return d ? d.of(cc.toUpperCase()) : cc.toUpperCase(); } catch (_) { return cc.toUpperCase(); } };
})();
const flag = (cc) => cc && /^[a-z]{2}$/i.test(cc) ? String.fromCodePoint(...[...cc.toUpperCase()].map((c) => 0x1f1a5 + c.charCodeAt(0))) : "🌐";

function vpn(method, params = {}) {
  const id = "q" + (++n);
  return new Promise((res) => { vpnWait.set(id, res); port.postMessage({ type: "vpn", id, method, params }); });
}
function vpnError(text) { $("vpn-err").hidden = !text; $("vpn-err").textContent = text || ""; }

function placesFor(b) {
  if (!providers) return [];
  if (b === "riseup") return (providers.riseup || []).map((l) => ({ id: l.id, label: `${l.city}, ${region(l.country)}`, cc: l.country }));
  if (b === "tor") return (providers.tor || []).map((t) => ({ id: t.id, label: region(t.country), cc: t.country }));
  return (providers.wireguard || []).map((w) => ({ id: w.id, label: w.label + (w.country ? ` · ${w.country}` : ""), cc: w.country }));
}

function renderPicker() {
  document.querySelectorAll("#vpn-backend button").forEach((b) => b.classList.toggle("on", b.dataset.b === backend));
  const places = placesFor(backend);
  const any = backend === "wireguard" ? [] : [`<option value="">${backend === "tor" ? "Any country" : "Any city"}</option>`];
  $("vpn-place").innerHTML = any.concat(places.map((p) => `<option value="${esc(p.id)}">${flag(p.cc)} ${esc(p.label)}</option>`)).join("")
    || '<option value="">Import a config first</option>';
  $("vpn-places").innerHTML = places.map((p) =>
    `<label><input type="checkbox" value="${esc(p.id)}"> ${flag(p.cc)} ${esc(p.label)}</label>`).join("");
  $("vpn-wg").hidden = backend !== "wireguard";
  $("vpn-wg-list").innerHTML = (providers && providers.wireguard || []).map((w) =>
    `<div><span>${flag(w.country)} ${esc(w.label)}</span><button data-rm="${esc(w.id)}">Remove</button></div>`).join("")
    || '<div><span class="note">None yet.</span></div>';
  let note = NOTES[backend];
  if (backend === "riseup" && providers && providers.riseup_error) note = "Riseup can't be reached right now: " + providers.riseup_error;
  $("vpn-note").textContent = note;
}

async function loadProviders() {
  const r = await vpn("providers");
  if (!r.ok) { vpnError(r.error); return; }
  providers = r;
  renderPicker();
}

function showVpn(st) {
  vst = st || {};
  const c = vst.connection;
  const down = vst.service === false;
  $("vpn-card").classList.toggle("up", !!c && c.state === "up");
  $("vpn-held").hidden = !vst.held;
  $("vpn-connect").hidden = !!c; $("vpn-disconnect").hidden = !c;
  const pill = $("vpn-pill"), dot = $("vpn-dot");
  if (down) {
    $("vpn-flag").textContent = "⚠️"; $("vpn-title").textContent = "The VPN service isn't running";
    $("vpn-sub").textContent = "neox-vpnd is not answering. Try again in a moment.";
    pill.textContent = "unavailable"; pill.className = "pill off"; dot.className = "dot bad";
  } else if (!c) {
    $("vpn-flag").textContent = vst.held ? "⛔" : "🌐";
    $("vpn-title").textContent = vst.held ? "VPN dropped" : "VPN is off";
    $("vpn-sub").textContent = vst.held ? "Pages are held back until you reconnect or turn it off." : "Pick a VPN and a place, then connect.";
    pill.textContent = vst.held ? "blocked" : "off"; pill.className = "pill" + (vst.held ? " off" : "");
    dot.className = "dot" + (vst.held ? " bad" : "");
    if (!exitIp) $("vpn-meta").innerHTML = "";
  } else {
    const where = c.city ? `${c.city}, ${region(c.country)}` : (c.country && c.country !== "ANY" ? region(c.country) : "Any country");
    $("vpn-flag").textContent = flag(c.country);
    $("vpn-title").textContent = where;
    $("vpn-sub").textContent = `${NAMES[c.backend] || c.backend} · ${c.scope === "neox" ? "only NEOX" : "whole computer"}`;
    pill.textContent = STATES[c.state] || c.state; pill.className = "pill " + (c.state === "up" ? "ok" : "busy");
    dot.className = "dot " + (c.state === "up" ? "on" : "busy");
    const rows = [];
    if (c.server) rows.push(`Server <b>${esc(c.server)}</b>`);
    if (exitIp) rows.push(`Exit IP <b>${esc(exitIp.ip || "?")}</b> · located in ${esc([exitIp.city, region(exitIp.country)].filter(Boolean).join(", ") || "unknown")}`);
    if (c.hop) {
      nextAt = Date.now() + c.hop.seconds_left * 1000;
      rows.push(`Next change in <b id="vpn-next"></b> · every ${c.hop.minutes} min`);
    } else nextAt = 0;
    $("vpn-meta").innerHTML = rows.map((r) => `<div>${r}</div>`).join("");
    tickNext();
    if (backend !== c.backend && !busy) { backend = c.backend; renderPicker(); }
  }
  if (!busy) vpnError(vst.error && !down ? vst.error : "");
}
function tickNext() {
  const el = document.getElementById("vpn-next");
  if (!el || !nextAt) return;
  const s = Math.max(0, Math.round((nextAt - Date.now()) / 1000));
  el.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
setInterval(tickNext, 1000);

async function refresh() { const r = await vpn("status"); if ("connected" in r || r.service === false) showVpn(r); }
function openVpn() {
  if (!providers) loadProviders();
  refresh();
  clearInterval(vpnPoll);
  vpnPoll = setInterval(() => { if ($("vpn").classList.contains("on") && !busy) refresh(); }, 5000);
}

function hopChoice() {
  if (!$("vpn-hop").checked) return null;
  const minutes = Number($("vpn-mins").value);
  const places = [...document.querySelectorAll("#vpn-places input:checked")].map((i) => i.value);
  if (!(minutes >= 1)) throw new Error("Change places at most once a minute (1 or more minutes).");
  if (places.length < 2) throw new Error("Tick at least two places to change between.");
  return { minutes, places, order: $("vpn-random").checked ? "random" : "cycle" };
}

async function connect(scope) {
  $("vpn-scope").hidden = true;
  let params;
  try { params = { backend, scope, place: $("vpn-place").value || null, hop: hopChoice() }; }
  catch (e) { vpnError(e.message); return; }
  busy = true; vpnError("");
  $("vpn-connect").disabled = true;
  $("vpn-title").textContent = "Connecting…"; $("vpn-pill").textContent = "connecting…"; $("vpn-pill").className = "pill busy";
  $("vpn-sub").textContent = backend === "tor" ? "Building Tor circuits; this can take a minute." : `${NAMES[backend]} · ${scope === "neox" ? "only NEOX" : "whole computer"}`;
  const r = await vpn("connect", params);
  busy = false; $("vpn-connect").disabled = false;
  if (r.ok) { chrome.storage.local.set({ vpnLast: params }); exitIp = null; showVpn(r); }
  else { await refresh(); vpnError(r.error || "Could not connect."); }
}

document.querySelectorAll("#vpn-backend button").forEach((b) => b.addEventListener("click", () => { backend = b.dataset.b; renderPicker(); }));
$("vpn-hop").addEventListener("change", () => { $("vpn-hop-opts").hidden = !$("vpn-hop").checked; });
$("vpn-connect").addEventListener("click", () => {
  if (backend === "wireguard" && !$("vpn-place").value) { vpnError("Import a WireGuard config first."); return; }
  try { hopChoice(); } catch (e) { vpnError(e.message); return; }
  vpnError(""); $("vpn-scope").hidden = false;
});
document.querySelectorAll("#vpn-scope button").forEach((b) => b.addEventListener("click", () => {
  if (b.dataset.s) connect(b.dataset.s); else $("vpn-scope").hidden = true;
}));
$("vpn-disconnect").addEventListener("click", async () => {
  const r = await vpn("disconnect");
  exitIp = null;
  if (r.ok) showVpn(r); else vpnError(r.error);
});
$("vpn-release").addEventListener("click", async () => { await vpn("release"); refresh(); });
$("vpn-again").addEventListener("click", async () => {
  const { vpnLast } = await chrome.storage.local.get("vpnLast");
  if (!vpnLast) { $("vpn-scope").hidden = false; return; }
  backend = vpnLast.backend; renderPicker();
  busy = true; const r = await vpn("connect", vpnLast); busy = false;
  if (r.ok) showVpn(r); else vpnError(r.error);
});
$("vpn-ip").addEventListener("click", async () => {
  $("vpn-ip").disabled = true;
  const r = await vpn("check_ip");
  $("vpn-ip").disabled = false;
  if (!r.ok) { vpnError(r.error); return; }
  exitIp = r;
  if (vst && vst.connection) showVpn(vst);
  else $("vpn-meta").innerHTML = `<div>Your IP <b>${esc(r.ip || "?")}</b> · located in ${esc([r.city, region(r.country)].filter(Boolean).join(", ") || "unknown")} (no VPN)</div>`;
});
$("vpn-wg-file").addEventListener("change", async (e) => {
  for (const f of e.target.files) {
    if (f.size > 16384) { vpnError(`${f.name} is too big to be a WireGuard config.`); continue; }
    const r = await vpn("import_wireguard", { filename: f.name, text: await f.text() });
    if (!r.ok) vpnError(`${f.name}: ${r.error}`);
  }
  e.target.value = "";
  await loadProviders();
});
$("vpn-wg-list").addEventListener("click", async (e) => {
  const id = e.target.dataset && e.target.dataset.rm;
  if (!id) return;
  const r = await vpn("remove_wireguard", { id });
  if (!r.ok) vpnError(r.error);
  await loadProviders();
});

connectPanel();
