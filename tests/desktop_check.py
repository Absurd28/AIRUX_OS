#!/usr/bin/env python3
"""Desktop check (Plasma, Windows 11 layout, AIRA panel, AIROX Code) on the live ISO in QEMU. Run as root.

Pass requires every line below; screenshots go to $AIRUX_WORK/build/vm/desktop-*.png.
  system      AIRUX, aira + aira-models-base + airox-code installed, AIRA's services healthy
  session     Plasma (plasmashell and KWin) runs for the live user
  taskbar     bottom panel: Start (AIRA icon), pinned apps incl. AIROX Code, tray, clock, show desktop
  theme       Breeze Dark from the AIRUX look-and-feel package
  aira_panel  AIRA's window is docked on the right edge, full height
  ptt         Ctrl+Space anywhere starts AIRA's microphone recording, pressing again stops it
  services    NetworkManager, bluetooth, power-profiles-daemon, upower, udisks2
  apps        Files, System Settings, AIROX Code, AIRA Settings and NEOX open a window
  airox_look  AIROX Code has its Midnight Glass stylesheet (checksums intact) and opens on AIRA Home
  aira_in_neox  a command to AIRA searches the web in a NEOX window
  tor         Tor is off at boot; a NEOX private window starts it and opens through it
"""
from __future__ import annotations

import re
import shlex
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
import phase8_check as p8  # noqa: E402

vm, D, sh = p8.vm, p8.D, p8.sh
USER = "airux"
W, H = vm.SCREEN


def session_env() -> str:
    """DISPLAY, XAUTHORITY, D-Bus and runtime dir of the live user's Plasma session."""
    pid = sh(f"pgrep -u {USER} -x plasmashell | head -1")["out"].strip()
    if not pid:
        return ""
    env = sh(f"tr '\\0' '\\n' < /proc/{pid}/environ")["out"]
    keep = [l for l in env.splitlines()
            if l.split("=", 1)[0] in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS",
                                      "XDG_RUNTIME_DIR", "HOME", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP")]
    return " ".join(f"'{l}'" for l in keep)


def as_user(env: str, cmd: str, timeout: float = 60) -> dict:
    return sh(f"runuser -u {USER} -- env {env} sh -c {shlex.quote(cmd)}", timeout=timeout)


LISTWIN_JS = """workspace.windowList().forEach(function (w) {
  var g = w.frameGeometry;
  print("TOKEN|" + w.caption + "|" + w.resourceClass + "|" + g.x + "|" + g.y + "|" + g.width + "|" + g.height + "|" + w.normalWindow);
});"""


APPROVE_PY = """import json, sys, urllib.request
port, token = sys.argv[1], sys.argv[2]
base = f"http://127.0.0.1:{port}"
for line in urllib.request.urlopen(f"{base}/events?token={token}"):
    if not line.startswith(b"data: "):
        continue
    ev = json.loads(line[6:])
    if ev.get("event") == "confirm" and ev.get("tool") in ("browser", "web_search"):
        req = urllib.request.Request(f"{base}/confirm", json.dumps({"id": ev["id"], "yes": True}).encode(),
                                     {"X-AIRA-Token": token, "Content-Type": "application/json"})
        print("approved", ev["args"], urllib.request.urlopen(req).read(), flush=True)"""


def kwin_script(env: str, js: str) -> None:
    """Run a one-shot KWin script in the live user's session."""
    sh("cat > /tmp/kwinone.js <<'JS'\n" + js + "\nJS\nchmod 644 /tmp/kwinone.js")
    sh("""cat > /tmp/kwinone.sh <<'SH'
#!/bin/sh
id=$(qdbus6 org.kde.KWin /Scripting org.kde.kwin.Scripting.loadScript /tmp/kwinone.js airux-oneshot)
qdbus6 org.kde.KWin /Scripting/Script$id org.kde.kwin.Script.run >/dev/null
sleep 1
qdbus6 org.kde.KWin /Scripting org.kde.kwin.Scripting.unloadScript airux-oneshot >/dev/null
SH
chmod 755 /tmp/kwinone.sh""")
    as_user(env, "/tmp/kwinone.sh")


def close_windows(env: str, keep: str = r"^AIRA$") -> None:
    """Close every normal window except AIRA's panel, as the user would with the close button."""
    kwin_script(env, "var keep = new RegExp(" + repr(keep) + ");\nworkspace.windowList().forEach(function (w) {\n"
                     "  if (w.normalWindow && w.caption && !keep.test(w.caption)) w.closeWindow();\n});")
    time.sleep(5)


def windows(env: str) -> list[tuple[str, int, int, int, int, str]]:
    """(title, x, y, width, height, class) of normal windows, asked from KWin itself (works on Wayland
    and X11): a one-shot KWin script prints them to the session journal."""
    token = f"AIRAWIN{int(time.time() * 1000)}"
    kwin_script(env, LISTWIN_JS.replace("TOKEN", token))
    out = as_user(env, f"journalctl --user -b --no-pager -o cat | grep '{token}|'")["out"]
    wins = []
    for line in out.splitlines():
        parts = line.split(f"{token}|", 1)[1].split("|")
        if len(parts) >= 7 and parts[6] == "true":
            wins.append((parts[0], int(parts[2]), int(parts[3]), int(parts[4]), int(parts[5]), parts[1]))
    return wins


def wait_window(env: str, pattern: str, timeout: float = 90):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        hits = [w for w in windows(env) if re.search(pattern, w[0]) and w[3] > 100]
        if hits:
            return max(hits, key=lambda w: w[3] * w[4])
        time.sleep(3)
    return None


def main() -> int:
    vm.stop(D)
    time.sleep(2)
    for f in ("desk.qcow2", "OVMF_VARS.fd", "audio.wav"):
        (D / f).unlink(missing_ok=True)
    vm.start(D, p8.ISO, D / "desk.qcow2", "cd")
    print(f"live system booted; guest agent after {p8.wait_agent():.0f}s", flush=True)
    r = {}
    base = p8.common_checks("live session")
    code = sh("dpkg-query -W -f='${Version} ${Status}' airox-code")["out"]
    r["system"] = base["ok"] and "install ok installed" in code
    print(f"airox-code: {code}")

    env = ""
    for _ in range(60):
        env = session_env()
        if env:
            break
        time.sleep(5)
    kwin = sh(f"pgrep -u {USER} -a 'kwin_(x11|wayland)' | head -1")["out"].strip()
    print(f"session env: {env}\nkwin: {kwin}")
    r["session"] = bool(env) and bool(kwin)
    if not env:
        print("=== desktop check: FAIL (no Plasma session)")
        return 1
    time.sleep(20)   # let the panel, AIRA and autostart settle
    vm.shot(D, D / "desktop-1-desktop.png")

    rc = as_user(env, "cat $HOME/.config/plasma-org.kde.plasma.desktop-appletsrc")["out"]
    plugins = re.findall(r"^plugin=(\S+)", rc, re.M)
    launchers = re.search(r"^launchers=(.*)$", rc, re.M)
    print(f"panel plugins: {plugins}\npinned: {launchers.group(1) if launchers else None}")
    need = ["org.kde.plasma.kickoff", "org.kde.plasma.icontasks", "org.kde.plasma.systemtray",
            "org.kde.plasma.digitalclock", "org.kde.plasma.showdesktop"]
    start_pins = sh("grep -o '<default>airox-code.desktop[^<]*' "
                    "/usr/share/plasma/plasmoids/org.kde.plasma.kickoff/contents/config/main.xml")["out"].strip()
    print(f"Start menu pinned apps: {start_pins.replace('<default>', '')}")
    r["taskbar"] = (all(n in plugins for n in need) and bool(launchers) and "airox-code.desktop" in launchers.group(1)
                    and "aira.desktop" in start_pins
                    and re.search(r"^icon=aira$", rc, re.M) is not None and re.search(r"^location=4$", rc, re.M) is not None)

    scheme = as_user(env, "kreadconfig6 --file kdeglobals --group General --key ColorScheme")["out"].strip()
    laf = as_user(env, "kreadconfig6 --file kdeglobals --group KDE --key LookAndFeelPackage")["out"].strip()
    print(f"colour scheme: {scheme}; look and feel: {laf}")
    r["theme"] = scheme == "BreezeDark" and laf == "org.airux.desktop"

    aira = wait_window(env, r"^AIRA$", 120)
    print(f"AIRA window: {aira}")
    if not aira:   # what AIRA's shell did, and what was on screen instead
        print(f"windows: {windows(env)}")
        print(as_user(env, "journalctl --user -b --no-pager -o short-monotonic | grep -i 'aira.shell\\|aira-shell' "
                           "| grep -v AIRAWIN | tail -15")["out"])
    r["aira_panel"] = bool(aira) and abs((aira[1] + aira[3]) - W) <= 12 and aira[4] >= H * 0.8 and aira[3] <= 480

    comp = as_user(env, "qdbus6 --literal org.kde.kglobalaccel /component/aira_ptt_desktop "
                        "org.kde.kglobalaccel.Component.allShortcutInfos")["out"]
    print("Ctrl+Space registered for aira-ptt:", "67108896" in comp)   # Qt key code of Ctrl+Space

    def recording() -> bool:
        return bool(sh(f"pgrep -u {USER} -x parecord")["out"].strip())
    toggles = []
    for _ in range(2):
        before = recording()
        vm.key(D, "ctrl-spc")
        after = before
        for _ in range(10):
            time.sleep(1)
            after = recording()
            if after != before:
                break
        toggles.append((before, after))
    print(f"Ctrl+Space presses (recording before, after): {toggles}")
    r["ptt"] = "67108896" in comp and all(b != a for b, a in toggles)
    if recording():
        vm.key(D, "ctrl-spc")

    units = ["NetworkManager", "bluetooth", "power-profiles-daemon", "upower", "udisks2"]
    st = {u: sh(f"systemctl is-enabled {u} 2>/dev/null; systemctl is-active {u}")["out"].split() for u in units}
    print(f"services: {st}")
    # bluetooth starts only with a Bluetooth adapter (the VM has none): enabled is what counts there
    r["services"] = all(v[:1] in (["enabled"], ["static"]) or "active" in v for v in st.values()) and \
        all("active" in st[u] for u in ("NetworkManager", "upower", "udisks2", "power-profiles-daemon"))

    vm.key(D, "meta_l")
    time.sleep(4)
    vm.shot(D, D / "desktop-2-start-menu.png")
    vm.key(D, "esc")
    time.sleep(2)

    apps = {"Files": ("dolphin", r"Dolphin"), "Settings": ("systemsettings", r"System Settings"),
            "AIROX Code": ("airox-code --new-window", r"AIRA Home|AIROX Code"),
            "AIRA Settings": ("aira-settings", r"^AIRA Settings$"), "NEOX": ("neox", r"New tab")}
    opened = {}
    for i, (name, (cmd, pat)) in enumerate(apps.items(), 3):
        as_user(env, f"setsid {cmd} >/dev/null 2>&1 &")
        w = wait_window(env, pat, 120)
        opened[name] = bool(w)
        if name == "AIRA Settings" and w:   # centred by the AIRUX KWin script, fully on screen
            opened[name] = w[1] >= 0 and w[2] >= 0 and w[1] + w[3] <= W and w[2] + w[4] <= H and w[3] >= 600
        print(f"{name}: {w}")
        time.sleep(3)
        vm.shot(D, D / f"desktop-{i}-{name.lower().replace(' ', '-')}.png")
    ver = as_user(env, "airox-code --version | head -1")["out"].strip()
    print(f"airox-code --version: {ver}")
    r["apps"] = all(opened.values()) and bool(ver)

    # AIROX Code's own look: the Midnight Glass stylesheet is in, every checksum still matches, and it
    # opened on AIRA Home
    look = sh("""python3 - <<'PY'
import base64, hashlib, json
app = "/usr/share/airox-code/resources/app"
p = json.load(open(app + "/product.json"))
d = lambda rel: base64.b64encode(hashlib.sha256(open(f"{app}/out/{rel}", "rb").read()).digest()).decode().rstrip("=")
css = open(app + "/out/vs/workbench/workbench.desktop.main.css").read()
print("styled" if "AIROX Code: Midnight Glass" in css else "plain", "checksums-ok" if all(d(k) == v for k, v in p["checksums"].items()) else "checksums-BAD")
PY""")["out"].strip()
    home = [w for w in windows(env) if "AIRA Home" in w[0]]
    print(f"AIROX Code look: {look}; AIRA Home open: {bool(home)}")
    r["airox_look"] = look == "styled checksums-ok" and bool(home)

    # AIRA browses in NEOX: a typed command to AIRA, and the search appears in a NEOX window. The apps
    # opened above are closed first (the 5 GB VM has no room for them and the model), so AIRA also
    # has to start NEOX itself.
    close_windows(env)
    print(f"windows left open: {windows(env)}")
    tok = as_user(env, "cat $HOME/.local/state/aira/shell.token")["out"].split()
    if len(tok) == 2:
        # opening a web page sends data off the machine, so AIRA asks first: answer "Yes" to its
        # browser confirmations as the user would click it in the panel (nothing else is approved)
        sh("cat > /tmp/approve.py <<'PY'\n" + APPROVE_PY + "\nPY\nchmod 644 /tmp/approve.py")
        as_user(env, f"setsid timeout 480 python3 /tmp/approve.py {tok[0]} {tok[1]} >/tmp/approve.log 2>&1 &")
        as_user(env, f"curl -s -m 10 -X POST -H 'X-AIRA-Token: {tok[1]}' -H 'Content-Type: application/json' "
                     f"-d '{{\"text\": \"Open the browser and search for debian trixie release notes\"}}' "
                     f"http://127.0.0.1:{tok[0]}/command")
    found = wait_window(env, r"(?i)trixie", 450)
    print(f"AIRA's search in NEOX: {found}")
    r["aira_in_neox"] = bool(found) and found[5] == "neox"   # NEOX's window (its --class), not AIRA's own panel
    time.sleep(3)
    vm.shot(D, D / "desktop-8-aira-in-neox.png")

    # private window with Tor: Tor is off until asked for, then the window goes through it
    close_windows(env)
    before = sh("systemctl is-enabled tor.service; systemctl is-active tor.service")["out"].split()
    # started the way the Start menu starts apps (in the user's systemd manager, so polkit sees the
    # desktop session); a process from outside the session is refused, as it should be
    as_user(env, "systemd-run --user --collect neox-private")
    tor_win = wait_window(env, r"(?i)tor", 180)
    after = sh("systemctl is-active tor.service")["out"].strip()
    print(f"Tor before: {before}; after opening a private window: {after}; window: {tor_win}")
    r["tor"] = before[:1] == ["disabled"] and after == "active" and bool(tor_win)
    vm.shot(D, D / "desktop-9-neox-tor.png")

    print("\n=== desktop check:", "PASS" if all(r.values()) else "FAIL", r)
    return 0 if all(r.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
