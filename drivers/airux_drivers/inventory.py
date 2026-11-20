"""The hardware of this computer, read from sysfs and /proc (never guessed).

Every reader takes `root` ("/" normally), so tests can point it at a fake tree that looks like a
real laptop's /sys and /proc.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Device:
    bus: str                    # pci | usb
    address: str                # 0000:00:14.3 | 1-4:1.0
    vendor: str                 # hex, lower case, no 0x
    device: str
    cls: str                    # pci: 6-digit class; usb: interface class/subclass/protocol "e00101"
    name: str = ""
    driver: str | None = None
    modalias: str = ""


@dataclass
class NetIf:
    name: str
    wireless: bool
    operstate: str
    address: str | None          # bus address of the device behind it
    driver: str | None


@dataclass
class Inventory:
    pci: list[Device] = field(default_factory=list)
    usb: list[Device] = field(default_factory=list)
    net: list[NetIf] = field(default_factory=list)
    rfkill: list[dict] = field(default_factory=list)     # {type, name, soft, hard}
    bluetooth: list[dict] = field(default_factory=list)  # {name, address, driver}
    gpus: list[dict] = field(default_factory=list)       # {card, address, driver, vendor, device}
    sound: list[dict] = field(default_factory=list)      # {index, name, driver}
    cameras: list[dict] = field(default_factory=list)    # {name, address}
    inputs: list[dict] = field(default_factory=list)     # {name, kind}
    batteries: list[dict] = field(default_factory=list)  # {name, capacity, status}
    firmware_failures: list[dict] = field(default_factory=list)   # {driver, address, file}
    secure_boot: str = "unknown"                         # enabled | disabled | unknown
    live: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _read(p: Path, default: str = "") -> str:
    try:
        return p.read_text(errors="replace").strip()
    except OSError:
        return default


def _driver(dev: Path) -> str | None:
    d = dev / "driver"
    try:
        return os.path.basename(os.readlink(d)) if d.is_symlink() else None
    except OSError:
        return None


# ---------------------------------------------------------------- names (pci.ids / usb.ids)
class IdNames:
    PATHS = {"pci": ("usr/share/misc/pci.ids", "usr/share/hwdata/pci.ids"),
             "usb": ("var/lib/usbutils/usb.ids", "usr/share/misc/usb.ids", "usr/share/hwdata/usb.ids")}

    def __init__(self, root: str):
        self.root, self.cache = root, {}

    def _load(self, bus: str) -> dict:
        if bus in self.cache:
            return self.cache[bus]
        table: dict = {}
        for rel in self.PATHS[bus]:
            p = Path(self.root, rel)
            if not p.exists():
                continue
            vendor = None
            for line in p.read_text(errors="replace").splitlines():
                if not line or line.startswith("#"):
                    continue
                if line.startswith("C "):           # device classes follow: done
                    break
                if not line.startswith("\t"):
                    vid, _, vname = line.partition("  ")
                    vendor = vid.strip().lower()
                    table[vendor] = (vname.strip(), {})
                elif not line.startswith("\t\t") and vendor:
                    did, _, dname = line.strip().partition("  ")
                    table[vendor][1][did.lower()] = dname.strip()
            break
        self.cache[bus] = table
        return table

    def name(self, bus: str, vendor: str, device: str) -> str:
        t = self._load(bus).get(vendor)
        if not t:
            return f"{bus.upper()} device {vendor}:{device}"
        dev = t[1].get(device)
        return f"{t[0]} {dev}" if dev else f"{t[0]} device {device}"


# ---------------------------------------------------------------- readers
def pci_devices(root: str, names: IdNames) -> list[Device]:
    out = []
    base = Path(root, "sys/bus/pci/devices")
    for d in sorted(base.glob("*")) if base.exists() else []:
        v = _read(d / "vendor").removeprefix("0x").lower()
        i = _read(d / "device").removeprefix("0x").lower()
        c = _read(d / "class").removeprefix("0x").lower()
        if not v:
            continue
        out.append(Device("pci", d.name, v, i, c, names.name("pci", v, i), _driver(d), _read(d / "modalias")))
    return out


def usb_devices(root: str, names: IdNames) -> list[Device]:
    """USB interfaces (that is where drivers bind), with their device's vendor and product."""
    out = []
    base = Path(root, "sys/bus/usb/devices")
    for d in sorted(base.glob("*:*")) if base.exists() else []:     # interfaces look like 1-4:1.0
        parent = d.parent / d.name.split(":")[0]
        if not parent.exists():
            parent = Path(os.path.realpath(d)).parent
        v = _read(parent / "idVendor").lower()
        i = _read(parent / "idProduct").lower()
        if not v:
            continue
        cls = _read(d / "bInterfaceClass") + _read(d / "bInterfaceSubClass") + _read(d / "bInterfaceProtocol")
        product = _read(parent / "product")
        name = names.name("usb", v, i)
        if product and "device" in name:
            name = product
        out.append(Device("usb", d.name, v, i, cls.lower(), name, _driver(d), _read(d / "modalias")))
    return out


BUS_ADDR = re.compile(r"^(?:[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]|\d+-[\d.]+:\d+\.\d+)$")


def _bus_address(dev_link: Path) -> str | None:
    """The PCI address or USB interface a class device belongs to. Some sit one level below it
    (a virtio network card's interface points at .../0000:00:03.0/virtio0), so walk up to it."""
    try:
        if not dev_link.exists():
            return None
        parts = Path(os.path.realpath(dev_link)).parts
    except OSError:
        return None
    for part in reversed(parts):
        if BUS_ADDR.match(part):
            return part
    return parts[-1] if parts else None


def net_interfaces(root: str) -> list[NetIf]:
    out = []
    base = Path(root, "sys/class/net")
    for d in sorted(base.glob("*")) if base.exists() else []:
        if d.name == "lo" or not (d / "device").exists():
            continue                                  # virtual interfaces (bridges, tun) have no device
        wireless = (d / "wireless").exists() or (d / "phy80211").exists()
        out.append(NetIf(d.name, wireless, _read(d / "operstate", "unknown"), _bus_address(d / "device"),
                         _driver(d / "device")))
    return out


def rfkill(root: str) -> list[dict]:
    out = []
    base = Path(root, "sys/class/rfkill")
    for d in sorted(base.glob("rfkill*")) if base.exists() else []:
        out.append({"type": _read(d / "type"), "name": _read(d / "name"),
                    "soft": _read(d / "soft") == "1", "hard": _read(d / "hard") == "1"})
    return out


def bluetooth(root: str) -> list[dict]:
    out = []
    base = Path(root, "sys/class/bluetooth")
    for d in sorted(base.glob("hci*")) if base.exists() else []:
        out.append({"name": d.name, "address": _bus_address(d / "device"), "driver": _driver(d / "device")})
    return out


def gpus(root: str) -> list[dict]:
    out = []
    base = Path(root, "sys/class/drm")
    for d in sorted(base.glob("card[0-9]")) if base.exists() else []:
        dev = d / "device"
        out.append({"card": d.name, "address": _bus_address(dev), "driver": _driver(dev),
                    "vendor": _read(dev / "vendor").removeprefix("0x").lower(),
                    "device": _read(dev / "device").removeprefix("0x").lower()})
    return out


def sound(root: str) -> list[dict]:
    out = []
    cards = _read(Path(root, "proc/asound/cards"))
    for m in re.finditer(r"^\s*(\d+)\s+\[[^\]]*\]:\s*(\S+)\s+-\s+(.+)$", cards, re.M):
        idx = m.group(1)
        out.append({"index": int(idx), "name": m.group(3).strip(), "driver": m.group(2),
                    "address": _bus_address(Path(root, f"sys/class/sound/card{idx}/device"))})
    return out


def cameras(root: str) -> list[dict]:
    out = []
    base = Path(root, "sys/class/video4linux")
    for d in sorted(base.glob("video*")) if base.exists() else []:
        idx = _read(d / "index", "0")
        if idx != "0":                               # one entry per camera (metadata nodes have index 1)
            continue
        out.append({"name": _read(d / "name"), "address": _bus_address(d / "device")})
    return out


def inputs(root: str) -> list[dict]:
    out = []
    text = _read(Path(root, "proc/bus/input/devices"))
    for block in text.split("\n\n"):
        m = re.search(r'^N: Name="(.*)"$', block, re.M)
        if not m:
            continue
        name = m.group(1)
        handlers = re.search(r"^H: Handlers=(.*)$", block, re.M)
        h = handlers.group(1) if handlers else ""
        if re.search(r"touchpad|trackpad|synaptics|elan.*tp|glidepoint", name, re.I):
            kind = "touchpad"
        elif re.search(r"touchscreen|wacom|stylus", name, re.I):
            kind = "touchscreen"
        elif "kbd" in h and re.search(r"keyboard", name, re.I):
            kind = "keyboard"
        elif "mouse" in h:
            kind = "mouse"
        else:
            continue
        out.append({"name": name, "kind": kind})
    return out


def batteries(root: str) -> list[dict]:
    out = []
    base = Path(root, "sys/class/power_supply")
    for d in sorted(base.glob("*")) if base.exists() else []:
        if _read(d / "type") == "Battery":
            out.append({"name": d.name, "capacity": _read(d / "capacity"), "status": _read(d / "status")})
    return out


# "iwlwifi 0000:00:14.3: Direct firmware load for iwlwifi-so-a0-gf-a0-89.ucode failed with error -2"
# "bluetooth hci0: Direct firmware load for intel/ibt-0040-0041.sfi failed with error -2"
# "amdgpu 0000:05:00.0: firmware: failed to load amdgpu/psp_13_0_4_toc.bin (-2)"
FW_RE = re.compile(r"(?:(?P<drv>[\w\-]+) (?P<addr>[\w:.\-]+): )?"
                   r"(?:Direct firmware load for (?P<f1>\S+) failed|firmware: failed to load (?P<f2>\S+))")


def firmware_failures(kernel_log: str) -> list[dict]:
    seen, out = set(), []
    for line in kernel_log.splitlines():
        m = FW_RE.search(line)
        if not m:
            continue
        f = m.group("f1") or m.group("f2")
        key = (m.group("drv"), m.group("addr"), f)
        if key in seen:
            continue
        seen.add(key)
        out.append({"driver": m.group("drv"), "address": m.group("addr"), "file": f})
    return out


def secure_boot(root: str) -> str:
    """From the EFI variable (no tools needed): enabled, disabled, or unknown (BIOS boot)."""
    vars_ = Path(root, "sys/firmware/efi/efivars")
    if not vars_.exists():
        return "unknown"
    for f in vars_.glob("SecureBoot-*"):
        try:
            data = f.read_bytes()
        except OSError:
            return "unknown"
        return "enabled" if data[-1:] == b"\x01" else "disabled"
    return "unknown"


KERNEL_JSON = "run/airux-drivers/kernel.json"


def read_kernel_failures(root: str) -> list[dict] | None:
    """Firmware failures collected at boot by the root service (users cannot read the kernel log)."""
    p = Path(root, KERNEL_JSON)
    try:
        return json.loads(p.read_text())["firmware_failures"]
    except (OSError, ValueError, KeyError):
        return None


def collect(root: str = "/", kernel_log: str | None = None) -> Inventory:
    names = IdNames(root)
    inv = Inventory(pci=pci_devices(root, names), usb=usb_devices(root, names), net=net_interfaces(root),
                    rfkill=rfkill(root), bluetooth=bluetooth(root), gpus=gpus(root), sound=sound(root),
                    cameras=cameras(root), inputs=inputs(root), batteries=batteries(root),
                    secure_boot=secure_boot(root),
                    live="boot=live" in _read(Path(root, "proc/cmdline")))
    if kernel_log is not None:
        inv.firmware_failures = firmware_failures(kernel_log)
    else:
        inv.firmware_failures = read_kernel_failures(root) or []
    return inv
