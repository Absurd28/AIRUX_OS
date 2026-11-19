// AIRUX: dock AIRA's window (a Chromium app window titled "AIRA") on the right edge of its screen,
// full height above the taskbar, like the Copilot panel in Windows 11. Each AIRA window is docked
// once, when it appears; after that the user may move or resize it freely.
// Its class is "aira" on X11 (chromium --class=aira); on Wayland Chromium ignores --class and the
// app id is "chrome-127.0.0.1__-Default", so the title and the local AIRA address are checked too.
const PANEL_WIDTH = 440;
const docked = new Set();

function isAira(w) {
    if (!w || !w.normalWindow || w.caption !== "AIRA") return false;
    const cls = String(w.resourceClass || "").toLowerCase();
    return cls === "aira" || cls.indexOf("chrome-127.0.0.1") === 0;
}

// AIRA Settings opens from the same Chromium process, which ignores --window-size then: centre it
function isAiraSettings(w) {
    if (!w || !w.normalWindow || w.caption !== "AIRA Settings") return false;
    const cls = String(w.resourceClass || "").toLowerCase();
    return cls === "aira-settings" || cls.indexOf("chrome-127.0.0.1__settings") === 0;
}

function dock(w) {
    if (docked.has(w.internalId)) return;
    const area = workspace.clientArea(KWin.MaximizeArea, w);
    if (isAira(w)) {
        docked.add(w.internalId);
        const width = Math.min(PANEL_WIDTH, Math.floor(area.width * 0.4));
        w.frameGeometry = { x: area.x + area.width - width, y: area.y, width: width, height: area.height };
    } else if (isAiraSettings(w)) {
        docked.add(w.internalId);
        const width = Math.min(1000, Math.floor(area.width * 0.85));
        const height = Math.min(720, Math.floor(area.height * 0.9));
        w.frameGeometry = { x: area.x + Math.floor((area.width - width) / 2), y: area.y + Math.floor((area.height - height) / 2),
                            width: width, height: height };
    }
}

function watch(w) {
    dock(w);
    // Chromium sets the page title a moment after the window appears
    w.captionChanged.connect(function () { dock(w); });
}

workspace.windowAdded.connect(watch);
workspace.windowRemoved.connect(function (w) { docked.delete(w.internalId); });
workspace.windowList().forEach(watch);
