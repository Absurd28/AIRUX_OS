"""AIRUX Drivers on fake sysfs/proc trees modelled on real machines (no root, no hardware)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from airux_drivers import actions, app, inventory
from airux_drivers.diagnose import FirmwareIndex, diagnose, nvidia_recommendation, summary

PCI_IDS = """\
8086  Intel Corporation
\t51f1  Raptor Lake PCH CNVi WiFi
\t51ca  Raptor Lake-P/U/H cAVS
\ta7a8  Raptor Lake-P [UHD Graphics]
\t293e  82801I (ICH9 Family) HD Audio Controller
10de  NVIDIA Corporation
\t28a1  AD107M [GeForce RTX 4050 Max-Q / Mobile]
\t1180  GK104 [GeForce GTX 680]
10ec  Realtek Semiconductor Co., Ltd.
\t8168  RTL8111/8168/8211/8411 PCI Express Gigabit Ethernet Controller
14c3  MEDIATEK Corp.
\t7961  MT7921 802.11ax PCI Express Wireless Network Adapter
1af4  Red Hat, Inc.
\t1000  Virtio network device
1234  QEMU
\t1111  QEMU Standard VGA
"""


class Machine:
    """Builds a fake / with the bits of sysfs and proc that inventory.py reads."""

    def __init__(self, root: Path):
        self.root = root
        (root / "usr/share/misc").mkdir(parents=True)
        (root / "usr/share/misc/pci.ids").write_text(PCI_IDS)
        (root / "proc").mkdir()
        (root / "proc/cmdline").write_text("BOOT_IMAGE=/vmlinuz quiet\n")
        self.dpkg: list[str] = []
        self.write_dpkg()

    def _driver(self, dev: Path, bus: str, driver: str | None):
        if driver:
            d = self.root / f"sys/bus/{bus}/drivers/{driver}"
            d.mkdir(parents=True, exist_ok=True)
            (dev / "driver").symlink_to(d)

    def pci(self, addr, vendor, device, cls, driver=None) -> Path:
        dev = self.root / "sys/devices/pci0000:00" / addr
        dev.mkdir(parents=True)
        for k, v in (("vendor", f"0x{vendor}"), ("device", f"0x{device}"), ("class", f"0x{cls}")):
            (dev / k).write_text(v + "\n")
        self._driver(dev, "pci", driver)
        link = self.root / "sys/bus/pci/devices" / addr
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(dev)
        return dev

    def usb(self, port, vendor, product, iface_cls, driver=None, name="") -> Path:
        base = self.root / "sys/devices/pci0000:00/0000:00:14.0/usb1"
        dev = base / port
        dev.mkdir(parents=True, exist_ok=True)
        (dev / "idVendor").write_text(vendor)
        (dev / "idProduct").write_text(product)
        if name:
            (dev / "product").write_text(name)
        itf = base / f"{port}:1.0"
        itf.mkdir()
        (itf / "bInterfaceClass").write_text(iface_cls[:2])
        (itf / "bInterfaceSubClass").write_text(iface_cls[2:4])
        (itf / "bInterfaceProtocol").write_text(iface_cls[4:6])
        self._driver(itf, "usb", driver)
        links = self.root / "sys/bus/usb/devices"
        links.mkdir(parents=True, exist_ok=True)
        (links / port).symlink_to(dev)
        (links / f"{port}:1.0").symlink_to(itf)
        return itf

    def cls_link(self, cls: str, entry: str, target: Path, **files):
        d = self.root / "sys/class" / cls / entry
        d.mkdir(parents=True)
        (d / "device").symlink_to(target)
        for k, v in files.items():
            if v is None:
                (d / k).mkdir()
            else:
                (d / k).write_text(v)
        return d

    def rfkill(self, idx, type_, soft=False, hard=False):
        d = self.root / "sys/class/rfkill" / f"rfkill{idx}"
        d.mkdir(parents=True)
        for k, v in (("type", type_), ("name", f"{type_}{idx}"), ("soft", str(int(soft))), ("hard", str(int(hard)))):
            (d / k).write_text(v)

    def sound_card(self, idx, driver, name, target: Path):
        p = self.root / "proc/asound/cards"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(f" {idx} [{driver[:6]:6}         ]: {driver} - {name}\n                      {name} at irq\n")
        self.cls_link("sound", f"card{idx}", target)

    def inputs(self, *names):
        p = self.root / "proc/bus/input/devices"
        p.parent.mkdir(parents=True, exist_ok=True)
        blocks = []
        for n, h in names:
            blocks.append(f'I: Bus=0018\nN: Name="{n}"\nH: Handlers={h}\n')
        p.write_text("\n".join(blocks))

    def battery(self, cap="81", status="Discharging"):
        d = self.root / "sys/class/power_supply/BAT1"
        d.mkdir(parents=True)
        (d / "type").write_text("Battery")
        (d / "capacity").write_text(cap)
        (d / "status").write_text(status)

    def secure_boot(self, on: bool):
        d = self.root / "sys/firmware/efi/efivars"
        d.mkdir(parents=True)
        (d / "SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c").write_bytes(b"\x06\x00\x00\x00" + bytes([on]))

    def write_dpkg(self, *pkgs):
        self.dpkg += list(pkgs)
        p = self.root / "var/lib/dpkg/status"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(f"Package: {x}\nStatus: install ok installed\n\n" for x in self.dpkg))


def acer_nitro(root: Path) -> tuple[Machine, str]:
    """Like an Acer Nitro ANV15-51: Intel CNVi Wi-Fi + Bluetooth, RTX 4050 on nouveau, Intel GPU,
    SOF audio, Realtek Ethernet, UVC camera, ELAN touchpad, battery, Secure Boot on."""
    m = Machine(root)
    m.pci("0000:00:14.3", "8086", "51f1", "028000", "iwlwifi")          # no wlan0: firmware missing
    m.pci("0000:00:02.0", "8086", "a7a8", "030000", "i915")
    gpu = m.pci("0000:00:02.0".replace("02.0", "02.1"), "10de", "28a1", "030200", "nouveau")
    sof = m.pci("0000:00:1f.3", "8086", "51ca", "040380", "sof-audio-pci-intel-tgl")
    eth = m.pci("0000:2c:00.0", "10ec", "8168", "020000", "r8169")
    m.cls_link("net", "enp44s0", eth, operstate="up")
    m.cls_link("drm", "card0", m.root / "sys/devices/pci0000:00/0000:00:02.0")
    m.cls_link("drm", "card1", gpu)
    m.usb("1-10", "8087", "0033", "e00101", "btusb", "Intel Bluetooth")            # no hci0: firmware missing
    cam = m.usb("1-6", "0408", "5473", "0e0100", "uvcvideo", "HD User Facing")
    m.cls_link("video4linux", "video0", cam, name="HD User Facing: HD User Facing", index="0")
    m.inputs(("ELAN0504:01 04F3:3091 Touchpad", "mouse0 event5"), ("AT Translated Set 2 keyboard", "sysrq kbd event2"))
    m.battery()
    m.secure_boot(True)
    log = ("iwlwifi 0000:00:14.3: Direct firmware load for iwlwifi-so-a0-gf-a0-89.ucode failed with error -2\n"
           "bluetooth hci0: Direct firmware load for intel/ibt-0040-0041.sfi failed with error -2\n"
           "sof-audio-pci-intel-tgl 0000:00:1f.3: Direct firmware load for intel/sof/sof-rpl.ri failed with error -2\n")
    return m, log


def by_cat(findings):
    out = {}
    for f in findings:
        out.setdefault(f.category, []).append(f)
    return out


def test_laptop_without_firmware_gets_the_right_fixes(tmp_path):
    m, log = acer_nitro(tmp_path)
    inv = inventory.collect(str(tmp_path), kernel_log=log)
    assert inv.secure_boot == "enabled" and len(inv.firmware_failures) == 3
    f = by_cat(diagnose(inv, FirmwareIndex(str(tmp_path))))
    wifi = f["wifi"][0]
    assert wifi.status == "needs_firmware" and wifi.fix.action == "install" and wifi.fix.args == ["firmware-iwlwifi"]
    assert "Raptor Lake PCH CNVi WiFi" in wifi.device
    bt = f["bluetooth"][0]
    assert bt.status == "needs_firmware" and bt.fix.args == ["firmware-iwlwifi"]          # Intel BT firmware lives there
    snd = f["sound"][0]
    assert snd.status == "needs_firmware" and snd.fix.args == ["firmware-sof-signed"]
    gfx = {x.device: x for x in f["graphics"]}
    nv = next(x for d, x in gfx.items() if "RTX 4050" in d)
    assert nv.status == "better_driver" and nv.fix.action == "nvidia"
    assert nv.fix.args == ["nvidia-driver", "nvidia-open-kernel-dkms"]
    intel = next(x for d, x in gfx.items() if "UHD" in d)
    assert intel.status == "working"
    assert f["ethernet"][0].status == "working" and "connected" in f["ethernet"][0].detail
    assert f["camera"][0].status == "working" and f["input"][0].status == "working"
    assert f["battery"][0].status == "working" and "81%" in f["battery"][0].detail
    sm = summary(diagnose(inv, FirmwareIndex(str(tmp_path))))
    assert not sm["all_working"] and sm["problems"] == 4 and sm["fixable"] == 4


def test_installed_but_too_old_firmware_points_to_backports(tmp_path):
    m, log = acer_nitro(tmp_path)
    m.write_dpkg("firmware-iwlwifi")
    inv = inventory.collect(str(tmp_path), kernel_log=log)
    wifi = by_cat(diagnose(inv, FirmwareIndex(str(tmp_path))))["wifi"][0]
    assert wifi.fix.action == "install_backports" and wifi.fix.args == ["firmware-iwlwifi"]


def test_working_mediatek_wifi_and_bluetooth_switched_off(tmp_path):
    m = Machine(tmp_path)
    w = m.pci("0000:01:00.0", "14c3", "7961", "028000", "mt7921e")
    m.cls_link("net", "wlp1s0", w, operstate="up", wireless=None)
    bt = m.usb("3-4", "0e8d", "0608", "e00101", "btusb", "Wireless_Device")
    m.cls_link("bluetooth", "hci0", bt)
    m.rfkill(0, "wlan")
    m.rfkill(1, "bluetooth", soft=True)
    f = by_cat(diagnose(inventory.collect(str(tmp_path), kernel_log=""), FirmwareIndex(str(tmp_path))))
    assert f["wifi"][0].status == "working" and "connected" in f["wifi"][0].detail
    assert f["bluetooth"][0].status == "off" and f["bluetooth"][0].fix.action == "unblock"
    m.rfkill(2, "wlan", hard=True)                         # the Wi-Fi key: only the user can press it
    f = by_cat(diagnose(inventory.collect(str(tmp_path), kernel_log=""), FirmwareIndex(str(tmp_path))))
    assert f["wifi"][0].status == "off" and f["wifi"][0].fix is None and "hardware switch" in f["wifi"][0].detail


def test_a_virtual_machine_has_nothing_to_fix(tmp_path):
    m = Machine(tmp_path)
    n = m.pci("0000:00:03.0", "1af4", "1000", "020000", "virtio-pci")
    (n / "virtio0").mkdir()                    # the interface hangs off a child device, as in QEMU
    m.cls_link("net", "enp0s3", n / "virtio0", operstate="up")
    g = m.pci("0000:00:01.0", "1234", "1111", "030000", "bochs-drm")
    m.cls_link("drm", "card0", g)
    a = m.pci("0000:00:1b.0", "8086", "293e", "040300", "snd_hda_intel")
    m.sound_card(0, "HDA-Intel", "HDA Intel at 0xfebf0000", a)
    m.inputs(("QEMU Virtio Keyboard", "sysrq kbd event1"))
    fs = diagnose(inventory.collect(str(tmp_path), kernel_log=""), FirmwareIndex(str(tmp_path)))
    f = by_cat(fs)
    assert {x.status for x in fs} <= {"working", "not_found"}
    assert f["sound"][0].status == "working" and f["graphics"][0].status == "working"
    assert f["wifi"][0].status == "not_found" and f["battery"][0].status == "not_found"
    assert summary(fs)["all_working"]


def test_old_nvidia_keeps_nouveau(tmp_path):
    m = Machine(tmp_path)
    m.pci("0000:01:00.0", "10de", "1180", "030000", "nouveau")
    g = by_cat(diagnose(inventory.collect(str(tmp_path), kernel_log=""), FirmwareIndex(str(tmp_path))))["graphics"][0]
    assert g.status == "working" and g.fix is None
    assert nvidia_recommendation("28a1")[0] == ["nvidia-driver", "nvidia-open-kernel-dkms"]
    assert nvidia_recommendation("1c03")[0] == ["nvidia-driver", "nvidia-kernel-dkms"]       # Pascal
    assert nvidia_recommendation("1180") is None


def test_firmware_files_map_to_packages(tmp_path):
    idx = FirmwareIndex(str(tmp_path), index_path=str(tmp_path / "none.json"))
    for fw, pkg in [("iwlwifi-so-a0-gf-a0-89.ucode", "firmware-iwlwifi"), ("mediatek/WIFI_RAM_CODE_MT7961_1.bin", "firmware-mediatek"),
                    ("rtw89/rtw8852b_fw.bin", "firmware-realtek"), ("ath11k/QCA6390/hw2.0/amss.bin", "firmware-atheros"),
                    ("amdgpu/psp_13_0_4_toc.bin", "firmware-amd-graphics"), ("i915/adlp_dmc.bin", "firmware-intel-graphics"),
                    ("intel/sof/sof-rpl.ri", "firmware-sof-signed"), ("brcm/brcmfmac43455-sdio.bin", "firmware-brcm80211"),
                    ("/lib/firmware/nvidia/ad107/gsp/booter_load-535.113.01.bin", "firmware-nvidia-graphics")]:
        assert idx.package(fw) == pkg, fw
    exact = tmp_path / "map.json"
    exact.write_text('{"intel/ibt-0040-0041.sfi.zst": "firmware-iwlwifi"}')
    assert FirmwareIndex(str(tmp_path), index_path=str(exact)).package("intel/ibt-0040-0041.sfi") == "firmware-iwlwifi"


def test_kernel_log_lines_are_read():
    log = ("[   5.1] amdgpu 0000:05:00.0: firmware: failed to load amdgpu/psp_13_0_4_toc.bin (-2)\n"
           "[   5.2] amdgpu 0000:05:00.0: firmware: failed to load amdgpu/psp_13_0_4_toc.bin (-2)\n"
           "some other line\n")
    assert inventory.firmware_failures(log) == [{"driver": "amdgpu", "address": "0000:05:00.0",
                                                 "file": "amdgpu/psp_13_0_4_toc.bin"}]


def test_the_helper_installs_only_drivers_and_firmware():
    for ok in ("firmware-iwlwifi", "nvidia-driver", "nvidia-open-kernel-dkms", "linux-headers-6.12.48+deb13-amd64",
               "intel-microcode", "printer-driver-gutenprint", "bluez-firmware"):
        assert actions.allowed(ok), ok
    for bad in ("bash", "openssh-server", "firmware-iwlwifi;rm", "nvidia-driver --reinstall", "../x", "linux-image-amd64"):
        assert not actions.allowed(bad), bad
    with pytest.raises(actions.ActionError, match="not allowed"):
        actions.check_packages(["firmware-iwlwifi", "netcat-openbsd"])


def test_sources_are_added_only_when_missing(tmp_path):
    (tmp_path / "etc/apt/sources.list.d").mkdir(parents=True)
    (tmp_path / "etc/apt/sources.list").write_text("deb http://deb.debian.org/debian/ trixie main non-free-firmware\n"
                                                   "deb http://deb.debian.org/debian/ trixie-updates main\n")
    text = actions.sources_needed(str(tmp_path))
    assert text and "contrib non-free non-free-firmware" in text and "trixie-backports" not in text
    (tmp_path / "etc/apt/sources.list").write_text(
        "deb [arch=amd64] http://deb.debian.org/debian/ trixie main contrib non-free non-free-firmware\n"
        "deb http://deb.debian.org/debian/ trixie-updates main contrib non-free non-free-firmware\n")
    assert actions.sources_needed(str(tmp_path)) is None
    assert "trixie-backports" in actions.sources_needed(str(tmp_path), backports=True)


def test_report_and_fix_commands(tmp_path, monkeypatch):
    m, log = acer_nitro(tmp_path)
    monkeypatch.setattr(inventory, "read_kernel_failures", lambda root: inventory.firmware_failures(log))
    st = app.status(str(tmp_path))
    text = app.report(st)
    assert "needs_firmware" in text and "iwlwifi-so-a0-gf-a0-89.ucode" in text and "Secure Boot: enabled" in text
    fix = app.find_fix("graphics", st)
    assert app.helper_cmd(fix) == ["pkexec", app.HELPER, "nvidia", "nvidia-driver", "nvidia-open-kernel-dkms"]
    assert app.find_fix("ethernet", st) is None
    with pytest.raises(ValueError):
        app.helper_cmd({"action": "rm", "args": ["-rf", "/"]})
