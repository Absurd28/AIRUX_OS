// NEOX background: the bridge between AIRA and this browser.
//   AIRA (agents) <-> Unix socket <-> native host (org.airux.neox) <-> this service worker <-> tabs
// AIRA's commands (open, search, read, click, back) run in the user's real NEOX window and return
// what the page now shows, so AIRA can read back every step. The side panel's questions go the
// other way: to AIRA, answered from the page's text.
"use strict";

const HOST = "org.airux.neox";
const SEARCH = "https://duckduckgo.com/?q=";
let port = null;
let retryMs = 1000;
const panels = new Set();           // open side panels (runtime ports)

function connectHost() {
  try { port = chrome.runtime.connectNative(HOST); } catch (e) { port = null; return scheduleReconnect(); }
  port.onMessage.addListener(onHostMessage);
  port.onDisconnect.addListener(() => {
    port = null;
    broadcast({ type: "status", aira: false, error: chrome.runtime.lastError && chrome.runtime.lastError.message });
    scheduleReconnect();
  });
  retryMs = 1000;
  broadcast({ type: "status", aira: true });
  vpnRequest("status", {}).catch(() => {});
}
function scheduleReconnect() { setTimeout(connectHost, retryMs); retryMs = Math.min(retryMs * 2, 30000); }
function send(msg) { if (port) port.postMessage(msg); }
function broadcast(msg) { for (const p of panels) { try { p.postMessage(msg); } catch (_) {} } }

// ---------------------------------------------------------------- pages AIRA works in
async function airaTab(create = true) {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true, windowType: "normal" });
  if (tab) return tab;
  const wins = await chrome.windows.getAll({ windowTypes: ["normal"] });
  if (wins.length) {
    const [t] = await chrome.tabs.query({ active: true, windowId: wins[0].id });
    if (t) return t;
  }
  if (!create) return null;
  const w = await chrome.windows.create({ url: "about:blank", focused: true });
  return w.tabs[0];
}

function waitLoaded(tabId, timeoutMs = 30000) {
  return new Promise((resolve) => {
    const done = () => { chrome.tabs.onUpdated.removeListener(listener); clearTimeout(timer); resolve(); };
    const listener = (id, info) => { if (id === tabId && info.status === "complete") done(); };
    const timer = setTimeout(done, timeoutMs);
    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs.get(tabId).then((t) => { if (t.status === "complete") done(); }).catch(done);
  });
}

// runs inside the page: what it shows now
function pageState(maxChars) {
  const text = (document.body ? document.body.innerText : "").replace(/\s+/g, " ").trim();
  const host = location.host;
  const anchors = [...document.querySelectorAll("a[href^='http']")];
  const outbound = anchors.filter((a) => !a.href.includes("://" + host + "/"));
  const links = anchors.filter((a) => a.innerText.trim()).slice(0, 40)
    .map((a) => ({ text: a.innerText.replace(/\s+/g, " ").trim().slice(0, 120), href: a.href })).slice(0, 10);
  return { url: location.href, title: document.title, text: text.slice(0, maxChars), outbound_links: outbound.length, links };
}

// runs inside the page: click the first visible element whose text contains `want`
function clickByText(want) {
  const w = want.toLowerCase();
  const cands = [...document.querySelectorAll("a, button, [role=button], input[type=submit], summary")];
  const el = cands.find((e) => (e.innerText || e.value || "").toLowerCase().includes(w) && e.offsetParent !== null);
  if (!el) return { clicked: false };
  el.click();
  return { clicked: true, text: (el.innerText || el.value || "").trim().slice(0, 120) };
}

// browser pages (about:, chrome:, the new tab) can't be scripted: say what is there, and that it has no text
async function stateOf(tabId, maxChars = 2000) {
  const t = await chrome.tabs.get(tabId);
  if (!/^(https?|file):/.test(t.url || "")) {
    return { url: t.url, title: t.title, text: "", outbound_links: 0, links: [], note: "a browser page with no readable text" };
  }
  const [r] = await chrome.scripting.executeScript({ target: { tabId }, func: pageState, args: [maxChars] });
  return r.result;
}

async function doCommand(c) {
  const tab = await airaTab();
  await chrome.windows.update(tab.windowId, { focused: true });
  if (c.cmd === "open" || c.cmd === "search") {
    let url = c.cmd === "search" ? SEARCH + encodeURIComponent(c.query) : (c.url || "about:blank");
    if (c.cmd === "open" && url && !/^(https?|about):/.test(url)) url = "https://" + url;
    await chrome.tabs.update(tab.id, { url, active: true });
    await new Promise((r) => setTimeout(r, 300));
    await waitLoaded(tab.id);
    const now = await chrome.tabs.get(tab.id);
    if (/^https?:/.test(url) && !/^https?:/.test(now.url || "")) throw new Error(`the page ${url} did not load`);
    return stateOf(tab.id);
  }
  if (c.cmd === "read") return stateOf(tab.id, 6000);
  if (c.cmd === "click") {
    const [r] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: clickByText, args: [c.text] });
    if (!r.result.clicked) throw new Error(`no link or button with the text "${c.text}" on this page`);
    await new Promise((res) => setTimeout(res, 500));
    await waitLoaded(tab.id);
    return stateOf(tab.id);
  }
  if (c.cmd === "back") {
    // the page's own history (tabs.goBack loses track after cross-site navigations)
    const before = (await chrome.tabs.get(tab.id)).url;
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: () => history.back() });
    for (let i = 0; i < 20 && (await chrome.tabs.get(tab.id)).url === before; i++) await new Promise((res) => setTimeout(res, 250));
    if ((await chrome.tabs.get(tab.id)).url === before) throw new Error("there is no earlier page in this tab");
    await waitLoaded(tab.id);
    return stateOf(tab.id);
  }
  throw new Error("unknown command " + c.cmd);
}

// ---------------------------------------------------------------- NEOX VPN
// The service (neox-vpnd) runs the tunnel; in "only NEOX" mode this browser reaches it through a
// SOCKS proxy. Kill switch: once you connect, NEOX keeps that proxy until *you* turn the VPN off, so
// if the tunnel or the service drops, pages stop loading instead of going out without the VPN.
let vpnState = null;                  // the last status from neox-vpnd
let vpnSeq = 0;
const vpnWaiters = new Map();

function vpnCall(method, params = {}) {
  return new Promise((resolve) => {
    if (!port) return resolve({ ok: false, error: "NEOX's helper isn't running.", service: false });
    const id = "v" + (++vpnSeq);
    vpnWaiters.set(id, resolve);
    send({ type: "vpn", id, method, params });
  });
}

async function setProxy(p) {
  if (p) {
    await chrome.proxy.settings.set({ scope: "regular", value: { mode: "fixed_servers",
      rules: { singleProxy: { scheme: p.scheme, host: p.host, port: p.port }, bypassList: ["localhost", "127.0.0.1", "[::1]"] } } });
  } else {
    await chrome.proxy.settings.clear({ scope: "regular" });
  }
}

async function applyVpn(st) {
  const { vpnHold } = await chrome.storage.local.get("vpnHold");
  const c = st && st.ok !== false ? st.connection : null;
  if (c && c.scope === "neox") {
    const p = c.proxy || vpnHold;                          // still connecting the first time: no proxy yet
    if (p) {
      await chrome.storage.local.set({ vpnHold: p });       // remembered for the kill switch
      await setProxy(p);
    }
  } else if (c) {                                           // whole computer: no proxy needed
    await chrome.storage.local.remove("vpnHold");
    await setProxy(null);
  } else if (vpnHold) {
    await setProxy(vpnHold);                                // dropped: keep blocking until you say otherwise
  } else {
    await setProxy(null);
  }
  vpnState = { ...(st || {}), held: !c && !!vpnHold };
  broadcast({ type: "vpn_state", state: vpnState });
}

async function vpnRequest(method, params) {
  const r = await vpnCall(method, params);
  if (method === "disconnect" && r.ok) await chrome.storage.local.remove("vpnHold");
  if ("connected" in r || r.service === false) {
    await applyVpn(r);
    return { ...r, held: vpnState.held };            // the panel shows the kill switch too
  }
  return r;
}

async function vpnRelease() {           // "Turn off" while the VPN is down: stop blocking
  await chrome.storage.local.remove("vpnHold");
  await vpnCall("disconnect");
  await applyVpn(await vpnCall("status"));
}

setInterval(() => { if (port) vpnRequest("status", {}).catch(() => {}); }, 10000);

async function onHostMessage(m) {
  if (m.type === "vpn_result") {
    const w = vpnWaiters.get(m.id);
    vpnWaiters.delete(m.id);
    if (w) w(m);
    return;
  }
  if (m.type === "command") {           // from AIRA's agents
    try { send({ type: "result", id: m.id, ok: true, value: await doCommand(m) }); }
    catch (e) { send({ type: "result", id: m.id, ok: false, error: String(e && e.message || e) }); }
    return;
  }
  // answers and AIRA events for the side panel
  broadcast(m);
}

// ---------------------------------------------------------------- side panel and new tab
chrome.runtime.onConnect.addListener((p) => {
  if (p.name !== "panel") return;
  panels.add(p);
  p.postMessage({ type: "status", aira: !!port });
  p.onDisconnect.addListener(() => panels.delete(p));
  if (vpnState) p.postMessage({ type: "vpn_state", state: vpnState });
  p.onMessage.addListener(async (m) => {
    if (m.type === "vpn") {
      const r = m.method === "release" ? (await vpnRelease(), { ok: true }) : await vpnRequest(m.method, m.params || {});
      return p.postMessage({ ...r, type: "vpn_result", id: m.id });
    }
    if (!port) return p.postMessage({ type: "error", id: m.id, text: "AIRA isn't reachable. Is AIRA running?" });
    if (m.type === "ask_page") {
      try {
        const tab = await airaTab(false);
        const page = tab ? await stateOf(tab.id, 14000) : null;
        if (!page || !page.text) return p.postMessage({ type: "error", id: m.id, text: "There is no readable page in this tab." });
        send({ type: "ask_page", id: m.id, question: m.question, page });
      } catch (e) {
        p.postMessage({ type: "error", id: m.id, text: "I can't read this page (" + (e.message || e) + ")." });
      }
    } else if (m.type === "aira_command") {
      send({ type: "aira_command", id: m.id, text: m.text });
    }
  });
});

chrome.runtime.onMessage.addListener((m, _sender, reply) => {
  if (m && m.type === "open_panel") {
    chrome.windows.getLastFocused().then((w) => chrome.sidePanel.open({ windowId: w.id })).then(() => reply(true), () => reply(false));
    return true;
  }
  if (m && m.type === "status") { reply({ aira: !!port, vpn: vpnState }); return false; }
});

chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
// Alt+Shift+A: the action's own shortcut does not open a side panel, so the command opens it. Called
// straight away in the handler (no await first): a keyboard shortcut counts as the user's gesture.
chrome.commands.onCommand.addListener((command, tab) => {
  if (command === "open-aira" && tab) chrome.sidePanel.open({ windowId: tab.windowId }).catch(() => {});
});
connectHost();
