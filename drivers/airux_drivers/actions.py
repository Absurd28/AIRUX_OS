"""What AIRUX Drivers may change, and how. The root helper (airux-drivers-helper, run through
pkexec and polkit) calls these; it checks every request against the allowlist itself.

  install            Debian packages from the allowlist (firmware, microcode, NVIDIA, printer drivers)
  install_backports  the same, newer, from trixie-backports (firmware for very new devices)
  nvidia             NVIDIA's driver + this kernel's headers (DKMS builds the module); reports
                     whether Secure Boot needs the module key enrolled
  unblock            rfkill unblock wifi | bluetooth
  collect            firmware failures from the kernel log -> /run/airux-drivers/kernel.json
  fwupd_refresh      fetch the device firmware catalogue (LVFS)
  fwupd_update       install device firmware updates
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from . import inventory

ALLOW = [r"firmware-[a-z0-9][a-z0-9\-]*", r"intel-microcode", r"amd64-microcode", r"bluez-firmware",
         r"nvidia-driver", r"nvidia-open-kernel-dkms", r"nvidia-kernel-dkms", r"linux-headers-amd64",
         r"linux-headers-\d[\w.+\-]*", r"printer-driver-[a-z0-9\-]+", r"hplip"]
COMPONENTS = {"main", "contrib", "non-free", "non-free-firmware"}
SOURCES_FILE = Path("/etc/apt/sources.list.d/airux-drivers.sources")
RUN_DIR = Path("/run/airux-drivers")
MOK_PUB = "/var/lib/dkms/mok.pub"


class ActionError(Exception):
    pass


def allowed(pkg: str) -> bool:
    return any(re.fullmatch(p, pkg) for p in ALLOW)


def check_packages(pkgs: list[str]) -> list[str]:
    if not pkgs:
        raise ActionError("no packages given")
    bad = [p for p in pkgs if not allowed(p)]
    if bad:
        raise ActionError(f"not allowed: {', '.join(bad)} (AIRUX Drivers installs only drivers and firmware)")
    return pkgs


def configured(root: str = "/", suite: str = "trixie") -> tuple[set[str], set[str]]:
    """(components, suites) apt already has for Debian's archive."""
    comps, suites = set(), set()
    files = [Path(root, "etc/apt/sources.list")] + sorted(Path(root, "etc/apt/sources.list.d").glob("*"))
    for f in files:
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        if f.suffix == ".sources":
            for stanza in text.split("\n\n"):
                kv = dict(line.split(":", 1) for line in stanza.splitlines() if ":" in line and not line.startswith("#"))
                if "deb.debian.org/debian" in kv.get("URIs", "") or "debian.org/debian" in kv.get("URIs", ""):
                    if "deb" in kv.get("Types", "deb"):
                        suites |= set(kv.get("Suites", "").split())
                        comps |= set(kv.get("Components", "").split())
        else:
            for line in text.splitlines():
                parts = line.split()
                if len(parts) >= 4 and parts[0] == "deb" and "debian" in line and "security" not in line:
                    i = 1
                    while parts[i].startswith("["):
                        i += 1
                    suites.add(parts[i + 1])
                    comps |= set(parts[i + 2:])
    return comps, suites


def sources_needed(root: str = "/", backports: bool = False) -> str | None:
    """The deb822 text AIRUX Drivers would add, or None if apt already has what is needed."""
    comps, suites = configured(root)
    want_suites = {"trixie", "trixie-updates"} | ({"trixie-backports"} if backports else set())
    if COMPONENTS <= comps and want_suites <= suites:
        return None
    return ("# Added by AIRUX Drivers: Debian's contrib, non-free and non-free-firmware sections (drivers and\n"
            "# firmware), and backports when newer firmware is needed.\n"
            "Types: deb\nURIs: http://deb.debian.org/debian\n"
            f"Suites: {' '.join(sorted(want_suites))}\n"
            "Components: main contrib non-free non-free-firmware\n"
            "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg\n")


def _run(cmd: list[str], log) -> int:
    log(f"$ {' '.join(cmd)}")
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive", LC_ALL="C.UTF-8")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    for line in p.stdout:
        log(line.rstrip())
    return p.wait()


def install(pkgs: list[str], log, backports: bool = False) -> dict:
    check_packages(pkgs)
    text = sources_needed("/", backports)
    if text:
        SOURCES_FILE.write_text(text)
        log(f"added {SOURCES_FILE}")
    if _run(["apt-get", "update"], log) != 0:
        raise ActionError("could not update the package lists (is the computer online?)")
    cmd = ["apt-get", "install", "-y", "--no-install-recommends"]
    if backports:
        cmd += ["-t", "trixie-backports"]
    if _run(cmd + pkgs, log) != 0:
        raise ActionError(f"installing {' '.join(pkgs)} failed")
    status = subprocess.run(["dpkg-query", "-W", "-f=${Package} ${Status}\\n"] + pkgs, capture_output=True, text=True)
    missing = [p for p in pkgs if f"{p} install ok installed" not in status.stdout]
    if missing:
        raise ActionError(f"not installed after all: {', '.join(missing)}")
    return {"installed": pkgs, "restart_needed": True}


def nvidia(pkgs: list[str], log) -> dict:
    check_packages(pkgs)
    if not any(p.startswith("nvidia-") for p in pkgs):
        raise ActionError("the NVIDIA action installs NVIDIA packages")
    headers = f"linux-headers-{os.uname().release}"
    out = install(pkgs + [headers], log)
    dkms = subprocess.run(["dkms", "status"], capture_output=True, text=True).stdout
    log(dkms.strip())
    built = bool(re.search(r"^nvidia.*installed", dkms, re.M | re.I))
    sb = inventory.secure_boot("/")
    out.update({"module_built": built, "secure_boot": sb,
                "enroll_key": sb == "enabled" and os.path.exists(MOK_PUB), "key": MOK_PUB})
    if not built:
        raise ActionError("the NVIDIA kernel module was not built (see the log above)")
    return out


def unblock(kind: str, log) -> dict:
    if kind not in ("wifi", "bluetooth"):
        raise ActionError("unblock wifi or bluetooth")
    if _run(["rfkill", "unblock", kind], log) != 0:
        raise ActionError(f"could not turn {kind} on")
    return {"unblocked": kind}


def collect(log=lambda s: None) -> dict:
    """Firmware failures from this boot's kernel log, readable by every user (the log itself is not)."""
    r = subprocess.run(["journalctl", "-k", "-b", "--no-pager", "-o", "cat"], capture_output=True, text=True)
    text = r.stdout if r.returncode == 0 else ""
    if not text:
        try:
            text = subprocess.run(["dmesg"], capture_output=True, text=True).stdout
        except OSError:
            text = ""
    fails = inventory.firmware_failures(text)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(RUN_DIR, 0o755)
    tmp = RUN_DIR / "kernel.json.tmp"
    tmp.write_text(json.dumps({"firmware_failures": fails}))
    os.chmod(tmp, 0o644)
    tmp.replace(RUN_DIR / "kernel.json")
    log(f"{len(fails)} firmware load failure(s) in the kernel log")
    return {"firmware_failures": fails}


def fwupd(update: bool, log) -> dict:
    if _run(["fwupdmgr", "refresh", "--force"], log) not in (0, 2):
        raise ActionError("could not fetch the firmware catalogue (is the computer online?)")
    if not update:
        return {"refreshed": True}
    rc = _run(["fwupdmgr", "update", "-y", "--no-reboot-check"], log)
    if rc not in (0, 2):                         # 2: nothing to do
        raise ActionError("a device firmware update failed (see the log)")
    return {"updated": rc == 0}
