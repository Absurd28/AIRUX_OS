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
  apps        Files (Dolphin), System Settings, AIROX Code and AIRA Settings open a window
"""
from __future__ import annotations

import re
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
    return sh(f"runuser -u {USER} -- env {env} sh -c {repr(cmd)}", timeout=timeout)


LISTWIN_JS = """workspace.windowList().forEach(function (w) {
  var g = w.frameGeometry;
  print("TOKEN|" + w.caption + "|" + w.resourceClass + "|" + g.x + "|" + g.y + "|" + g.width + "|" + g.height + "|" + w.normalWindow);
});"""


def windows(env: str) -> list[tuple[str, int, int, int, int, str]]:
    """(title, x, y, width, height, class) of normal windows, asked from KWin itself (works on Wayland
    and X11): a one-shot KWin script prints them to the session journal."""
    token = f"AIRAWIN{int(time.time() * 1000)}"
    sh("cat > /tmp/listwin.js <<'JS'\n" + LISTWIN_JS.replace("TOKEN", token) + "\nJS\nchmod 644 /tmp/listwin.js")
    sh("""cat > /tmp/listwin.sh <<'SH'
#!/bin/sh
id=$(qdbus6 org.kde.KWin /Scripting org.kde.kwin.Scripting.loadScript /tmp/listwin.js airux-listwin)
qdbus6 org.kde.KWin /Scripting/Script$id org.kde.kwin.Script.run >/dev/null
sleep 1
qdbus6 org.kde.KWin /Scripting org.kde.kwin.Scripting.unloadScript airux-listwin >/dev/null
SH
chmod 755 /tmp/listwin.sh""")
    as_user(env, "/tmp/listwin.sh")
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
            "AIROX Code": ("airox-code --new-window", r"AIROX Code"), "AIRA Settings": ("aira-settings", r"^AIRA Settings$")}
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

    print("\n=== desktop check:", "PASS" if all(r.values()) else "FAIL", r)
    return 0 if all(r.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
