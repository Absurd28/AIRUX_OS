"""From the inventory to a verdict per category, with the fix when there is one.

Statuses:
  working         a driver is bound and the device is usable
  needs_firmware  the driver could not load a firmware file (fix: install its package, or a newer one)
  off             switched off by rfkill (fix: turn it on; a hardware switch needs the user)
  better_driver   works with a basic driver, a vendor driver is recommended (NVIDIA)
  no_driver       the device is there but no driver took it
  not_found       this computer has no such device
"""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .inventory import Device, Inventory

CATEGORIES = [("wifi", "Wi-Fi"), ("ethernet", "Ethernet"), ("bluetooth", "Bluetooth"), ("graphics", "Graphics"),
              ("sound", "Sound"), ("camera", "Camera"), ("input", "Touchpad and keyboard"), ("battery", "Battery")]

# firmware path -> Debian package, for files missing from the exact index (built at package time from
# Debian's Contents index: firmware-map.json); first match wins
FW_RULES = [
    ("iwlwifi-*", "firmware-iwlwifi"), ("intel/ibt-*", "firmware-iwlwifi"), ("intel/iwlwifi/*", "firmware-iwlwifi"),
    ("mediatek/*", "firmware-mediatek"), ("mt76*", "firmware-mediatek"),
    ("rtw88/*", "firmware-realtek"), ("rtw89/*", "firmware-realtek"), ("rtl_bt/*", "firmware-realtek"),
    ("rtl_nic/*", "firmware-realtek"), ("rtlwifi/*", "firmware-realtek"), ("rtl8*", "firmware-realtek"),
    ("ath9k_htc/*", "firmware-atheros"), ("ath10k/*", "firmware-atheros"), ("ath11k/*", "firmware-atheros"),
    ("ath12k/*", "firmware-atheros"), ("qca/*", "firmware-atheros"), ("ar3k/*", "firmware-atheros"),
    ("brcm/*", "firmware-brcm80211"), ("cypress/*", "firmware-brcm80211"),
    ("amdgpu/*", "firmware-amd-graphics"), ("radeon/*", "firmware-amd-graphics"),
    ("i915/*", "firmware-intel-graphics"), ("xe/*", "firmware-intel-graphics"),
    ("nvidia/*", "firmware-nvidia-graphics"),
    ("intel/sof*", "firmware-sof-signed"), ("intel/avs/*", "firmware-intel-sound"), ("intel/*", "firmware-intel-misc"),
    ("libertas/*", "firmware-libertas"), ("ti-connectivity/*", "firmware-ti-connectivity"),
    ("cirrus/*", "firmware-cirrus"), ("*", "firmware-misc-nonfree"),
]
WIFI_DRIVERS = {"iwlwifi", "mt7921e", "mt7921u", "mt7925e", "mt7922", "mt76x2e", "mt7915e", "rtw88_8822ce",
                "rtw88_8821ce", "rtw88_8822be", "rtw89_8852ae", "rtw89_8852be", "rtw89_8852ce", "rtl8821ce", "rtl8xxxu",
                "ath9k", "ath10k_pci", "ath11k_pci", "ath12k_pci", "brcmfmac", "brcmsmac", "wl", "b43", "iwlmvm",
                "rtw88_8723de", "rtw89_8851be", "mt7996e"}
BT_DRIVERS = {"btusb", "btintel", "btmtk", "btrtl", "btbcm", "hci_uart", "btqca", "bluetooth"}
GPU_DRIVERS_OK = {"i915", "xe", "amdgpu", "radeon", "nvidia", "virtio-pci", "virtio_gpu", "bochs-drm", "bochs",
                  "qxl", "vmwgfx", "simpledrm", "ast", "mgag200"}


@dataclass
class Fix:
    action: str                      # install | install_backports | unblock | nvidia | switch
    args: list[str] = field(default_factory=list)
    label: str = ""
    needs_restart: bool = False


@dataclass
class Finding:
    category: str
    title: str
    device: str
    status: str
    detail: str
    fix: Fix | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


class FirmwareIndex:
    def __init__(self, root: str = "/", index_path: str | None = None):
        self.root = root
        self.exact: dict[str, str] = {}
        p = Path(index_path) if index_path else Path(root, "usr/share/airux-drivers/firmware-map.json")
        try:
            self.exact = json.loads(p.read_text())
        except (OSError, ValueError):
            pass
        self.installed = installed_packages(root)

    def package(self, fw: str) -> str:
        fw = fw.lstrip("/").removeprefix("lib/firmware/").removeprefix("usr/lib/firmware/")
        for cand in (fw, fw + ".zst", fw + ".xz"):
            if cand in self.exact:
                return self.exact[cand]
        for pat, pkg in FW_RULES:
            if fnmatch.fnmatch(fw, pat):
                return pkg
        return "firmware-misc-nonfree"

    def fix_for(self, fw: str) -> Fix:
        pkg = self.package(fw)
        if pkg in self.installed:
            # the package is here but this device needs a newer file than Debian 13 shipped
            return Fix("install_backports", [pkg], f"Install a newer {pkg} (from Debian backports)", True)
        return Fix("install", [pkg], f"Install {pkg}", True)


def installed_packages(root: str) -> set[str]:
    out, pkg = set(), None
    try:
        text = Path(root, "var/lib/dpkg/status").read_text(errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        if line.startswith("Package: "):
            pkg = line[9:].strip()
        elif line.startswith("Status: ") and pkg and line.endswith(" installed"):
            out.add(pkg)
    return out


def _fails_for(inv: Inventory, address: str | None, drivers: set[str] | None = None) -> list[dict]:
    out = []
    for f in inv.firmware_failures:
        if address and f.get("address") == address:
            out.append(f)
        elif drivers and f.get("driver") in drivers:
            out.append(f)
    return out


def nvidia_recommendation(device_id: str) -> tuple[list[str], str] | None:
    """Debian 13 packages for an NVIDIA GPU, by generation (from the PCI device id)."""
    try:
        n = int(device_id, 16)
    except ValueError:
        return None
    if n >= 0x1e00:      # Turing (RTX 20 / GTX 16) and newer, incl. RTX 40 laptops: open kernel modules
        return ["nvidia-driver", "nvidia-open-kernel-dkms"], "NVIDIA driver with open kernel modules (recommended for RTX)"
    if n >= 0x1340:      # Maxwell and Pascal
        return ["nvidia-driver", "nvidia-kernel-dkms"], "NVIDIA driver"
    return None          # Kepler and older: legacy drivers only; nouveau stays


def diagnose(inv: Inventory, fw: FirmwareIndex) -> list[Finding]:
    out: list[Finding] = []
    title = dict(CATEGORIES)

    # ---- Wi-Fi
    wifi_pci = [d for d in inv.pci if d.cls.startswith("0280")]
    wifi_usb = [d for d in inv.usb if d.driver in WIFI_DRIVERS]
    wl_ifs = [n for n in inv.net if n.wireless]
    blocked = [r for r in inv.rfkill if r["type"] == "wlan" and (r["soft"] or r["hard"])]
    devices = wifi_pci + wifi_usb
    if not devices and not wl_ifs:
        out.append(Finding("wifi", title["wifi"], "", "not_found", "This computer has no Wi-Fi adapter."))
    for n in wl_ifs if not devices else []:          # an adapter we could not classify, but it works
        out.append(Finding("wifi", title["wifi"], n.name, "off" if blocked else "working",
                           f"{n.name} ({n.driver})" + (", switched off." if blocked else ".")))
    for d in devices:
        iface = next((n for n in wl_ifs if n.address == d.address), None)
        fails = _fails_for(inv, d.address, {d.driver} if d.driver else None)
        if blocked:
            hard = any(r["hard"] for r in blocked)
            out.append(Finding("wifi", title["wifi"], d.name, "off",
                               "Wi-Fi is switched off" + (" by a hardware switch or key (Fn+F3 on many laptops)."
                                                          if hard else "."),
                               None if hard else Fix("unblock", ["wifi"], "Turn Wi-Fi on")))
        elif iface:
            out.append(Finding("wifi", title["wifi"], d.name, "working",
                               f"{iface.name} ({d.driver}), {'connected' if iface.operstate == 'up' else 'not connected'}."))
        elif fails:
            f = fails[0]
            out.append(Finding("wifi", title["wifi"], d.name, "needs_firmware",
                               f"{d.driver or 'The driver'} could not load {f['file']}.", fw.fix_for(f["file"])))
        elif d.driver:
            out.append(Finding("wifi", title["wifi"], d.name, "no_driver",
                               f"{d.driver} is loaded but no Wi-Fi interface appeared; the kernel log may say why."))
        else:
            out.append(Finding("wifi", title["wifi"], d.name, "no_driver",
                               f"No driver took this adapter ({d.vendor}:{d.device}); it may be too new for this kernel."))

    # ---- Ethernet
    eth = [d for d in inv.pci if d.cls.startswith("0200")]
    if not eth:
        out.append(Finding("ethernet", title["ethernet"], "", "not_found", "No wired network port."))
    for d in eth:
        iface = next((n for n in inv.net if n.address == d.address), None)
        fails = _fails_for(inv, d.address)
        if iface:
            out.append(Finding("ethernet", title["ethernet"], d.name, "working",
                               f"{iface.name} ({d.driver}), cable {'connected' if iface.operstate == 'up' else 'unplugged'}."))
        elif fails:
            out.append(Finding("ethernet", title["ethernet"], d.name, "needs_firmware",
                               f"{d.driver or 'The driver'} could not load {fails[0]['file']}.",
                               fw.fix_for(fails[0]["file"])))
        else:
            out.append(Finding("ethernet", title["ethernet"], d.name, "no_driver", "No driver took this port."))

    # ---- Bluetooth
    bt_usb = [d for d in inv.usb if d.cls.startswith("e00101")]
    bt_block = [r for r in inv.rfkill if r["type"] == "bluetooth" and (r["soft"] or r["hard"])]
    bt_fail = [f for f in inv.firmware_failures if f.get("driver") in BT_DRIVERS
               or (f.get("address") or "").startswith("hci") or "ibt-" in f["file"] or "rtl_bt" in f["file"]]
    if inv.bluetooth and bt_block:
        hard = any(r["hard"] for r in bt_block)
        out.append(Finding("bluetooth", title["bluetooth"], inv.bluetooth[0]["name"], "off", "Bluetooth is switched off.",
                           None if hard else Fix("unblock", ["bluetooth"], "Turn Bluetooth on")))
    elif bt_fail:
        f = bt_fail[0]
        name = bt_usb[0].name if bt_usb else "Bluetooth adapter"
        out.append(Finding("bluetooth", title["bluetooth"], name, "needs_firmware",
                           f"Bluetooth could not load {f['file']}.", fw.fix_for(f["file"])))
    elif inv.bluetooth:
        out.append(Finding("bluetooth", title["bluetooth"], inv.bluetooth[0]["name"], "working",
                           f"{inv.bluetooth[0]['name']} ({inv.bluetooth[0]['driver']})."))
    elif bt_usb:
        out.append(Finding("bluetooth", title["bluetooth"], bt_usb[0].name, "no_driver",
                           "A Bluetooth adapter is there but did not start."))
    else:
        out.append(Finding("bluetooth", title["bluetooth"], "", "not_found", "No Bluetooth adapter."))

    # ---- Graphics
    gpu_pci = [d for d in inv.pci if d.cls[:4] in ("0300", "0302", "0380")]
    if not gpu_pci:
        out.append(Finding("graphics", title["graphics"], "", "not_found", "No graphics adapter found."))
    for d in gpu_pci:
        fails = _fails_for(inv, d.address, {d.driver} if d.driver else None)
        if d.vendor == "10de" and d.driver != "nvidia":
            rec = nvidia_recommendation(d.device)
            if rec:
                out.append(Finding("graphics", title["graphics"], d.name, "better_driver",
                                   f"Running on {d.driver or 'no driver'}; NVIDIA's driver gives full speed, "
                                   "power management and CUDA (AIRA then uses the GPU).",
                                   Fix("nvidia", rec[0], f"Install the {rec[1]}", True)))
                continue
        if fails:
            out.append(Finding("graphics", title["graphics"], d.name, "needs_firmware",
                               f"{d.driver or 'The driver'} could not load {fails[0]['file']}.",
                               fw.fix_for(fails[0]["file"])))
        elif d.driver:
            out.append(Finding("graphics", title["graphics"], d.name, "working", f"Driver: {d.driver}."))
        else:
            out.append(Finding("graphics", title["graphics"], d.name, "no_driver",
                               "No driver took this graphics adapter; the screen runs on a basic one."))

    # ---- Sound
    audio = [d for d in inv.pci if d.cls[:4] in ("0403", "0401")]
    snd_fail = [f for f in inv.firmware_failures if "sof" in (f.get("driver") or "") or "sof" in f["file"]
                or (f.get("driver") or "").startswith("snd")]
    if inv.sound:
        names = ", ".join(c["name"] for c in inv.sound)
        out.append(Finding("sound", title["sound"], names, "working", f"{len(inv.sound)} sound card(s): {names}."))
    elif snd_fail:
        f = snd_fail[0]
        out.append(Finding("sound", title["sound"], audio[0].name if audio else "Audio", "needs_firmware",
                           f"The sound driver could not load {f['file']} (laptop speakers and microphones).",
                           fw.fix_for(f["file"])))
    elif audio:
        out.append(Finding("sound", title["sound"], audio[0].name, "no_driver", "An audio device is there but no sound card started."))
    else:
        out.append(Finding("sound", title["sound"], "", "not_found", "No sound hardware found."))

    # ---- Camera
    cam_usb = [d for d in inv.usb if d.cls.startswith("0e01")]          # USB video class, control interface
    if inv.cameras:
        out.append(Finding("camera", title["camera"], inv.cameras[0]["name"], "working", "Camera is ready."))
    elif cam_usb:
        d = cam_usb[0]
        out.append(Finding("camera", title["camera"], d.name, "no_driver" if not d.driver else "working",
                           "Camera found but not started." if not d.driver else f"Driver: {d.driver}."))
    else:
        ipu = [d for d in inv.pci if d.cls.startswith("0480") and d.vendor == "8086"]
        out.append(Finding("camera", title["camera"], ipu[0].name if ipu else "",
                           "no_driver" if ipu else "not_found",
                           "This laptop's camera uses Intel's IPU (MIPI), which Debian 13 does not support yet."
                           if ipu else "No camera found."))

    # ---- Input
    pads = [i for i in inv.inputs if i["kind"] in ("touchpad", "touchscreen")]
    kbds = [i for i in inv.inputs if i["kind"] == "keyboard"]
    if pads or kbds:
        out.append(Finding("input", title["input"], ", ".join(i["name"] for i in pads + kbds), "working",
                           f"{len(pads)} touchpad/touchscreen, {len(kbds)} keyboard(s)."))
    else:
        out.append(Finding("input", title["input"], "", "not_found", "No touchpad or keyboard reported."))

    # ---- Battery
    if inv.batteries:
        b = inv.batteries[0]
        out.append(Finding("battery", title["battery"], b["name"], "working", f"{b['capacity']}% ({b['status']})."))
    else:
        out.append(Finding("battery", title["battery"], "", "not_found", "No battery (desktop or VM)."))
    return out


def summary(findings: list[Finding]) -> dict:
    bad = [f for f in findings if f.status in ("needs_firmware", "off", "no_driver", "better_driver")]
    return {"all_working": not bad, "problems": len(bad),
            "fixable": sum(1 for f in bad if f.fix is not None)}
