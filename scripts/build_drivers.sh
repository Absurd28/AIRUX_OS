#!/usr/bin/env bash
# Build airux-drivers_<version>_all.deb: AIRUX Drivers (hardware status, firmware and NVIDIA driver
# installs, the hardware report). Output: $AIRUX_WORK/build/deb. Run as root. Standard library only.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${AIRUX_WORK:-/srv/airux-work}"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$REPO/drivers/airux_drivers/__init__.py")"
OUT="$WORK/build/deb"
STAGE="$OUT/airux-drivers"
D="$REPO/drivers"
P="$D/packaging"
CONTENTS="$WORK/downloads/trixie-non-free-firmware-Contents-all.gz"

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
mkdir -p "$OUT" "$WORK/downloads"
rm -rf "$STAGE"

echo "== checks: every module compiles"
python3 -m py_compile "$D"/airux_drivers/*.py

echo "== firmware file -> package map (Debian's non-free-firmware Contents index)"
install -d "$STAGE/usr/share/airux-drivers"
if [ ! -s "$CONTENTS" ] || [ -n "$(find "$CONTENTS" -mtime +30 2>/dev/null)" ]; then
    curl -fsSL -o "$CONTENTS.part" "https://deb.debian.org/debian/dists/trixie/non-free-firmware/Contents-all.gz" \
        && mv "$CONTENTS.part" "$CONTENTS" || echo "could not download the Contents index; using the built-in rules only"
fi
python3 - "$CONTENTS" "$STAGE/usr/share/airux-drivers/firmware-map.json" <<'PY'
import gzip, json, os, sys
src, dst = sys.argv[1], sys.argv[2]
m = {}
if os.path.exists(src):
    with gzip.open(src, "rt", errors="replace") as f:
        for line in f:
            path, _, loc = line.rstrip("\n").rpartition(" ")
            path = path.strip()
            for prefix in ("usr/lib/firmware/", "lib/firmware/"):
                if path.startswith(prefix):
                    m[path[len(prefix):]] = loc.split("/")[-1].split(",")[0]
json.dump(m, open(dst, "w"), separators=(",", ":"), sort_keys=True)
print(f"{len(m)} firmware files mapped")
PY

echo "== files"
install -d "$STAGE/usr/lib/airux-drivers" "$STAGE/usr/bin" "$STAGE/usr/lib/systemd/system" \
    "$STAGE/usr/share/polkit-1/actions" "$STAGE/usr/share/applications" "$STAGE/usr/share/doc/airux-drivers"
( cd "$D" && tar --exclude=__pycache__ --exclude='*.pyc' -cf - airux_drivers ) | tar -xf - -C "$STAGE/usr/lib/airux-drivers"
install -m755 "$P/bin/airux-drivers" "$P/bin/airux-drivers-app" "$STAGE/usr/bin/"
install -m755 "$P/bin/airux-drivers-helper" "$STAGE/usr/lib/airux-drivers/"
install -m644 "$P"/systemd/* "$STAGE/usr/lib/systemd/system/"
install -m644 "$P/polkit/org.airux.drivers.policy" "$STAGE/usr/share/polkit-1/actions/"
install -m644 "$P/applications/airux-drivers.desktop" "$STAGE/usr/share/applications/"
install -Dm644 "$D/airux_drivers/ui/airux-drivers.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/airux-drivers.svg"
for s in 16 22 24 32 48 64 128 256; do
    install -d "$STAGE/usr/share/icons/hicolor/${s}x${s}/apps"
    rsvg-convert -w $s -h $s "$D/airux_drivers/ui/airux-drivers.svg" -o "$STAGE/usr/share/icons/hicolor/${s}x${s}/apps/airux-drivers.png"
done
cat > "$STAGE/usr/share/doc/airux-drivers/README" <<'DOC'
AIRUX Drivers checks this computer's hardware (from sysfs and the kernel's firmware errors) and
fixes what it can: installs missing firmware (also newer firmware from Debian backports), NVIDIA's
driver (with the Secure Boot key enrollment), switches Wi-Fi/Bluetooth on, and device firmware
updates through fwupd. It never writes or loads drivers of its own: Linux's drivers are in the
kernel. Root actions go through /usr/lib/airux-drivers/airux-drivers-helper (pkexec, polkit
org.airux.drivers.manage), which installs only from an allowlist. Licence: MIT (part of AIRUX).
DOC

echo "== package"
chown -R root:root "$STAGE"
install -d "$STAGE/DEBIAN"
install -m755 "$P/debian/postinst" "$P/debian/prerm" "$STAGE/DEBIAN/"
cat > "$STAGE/DEBIAN/control" <<CTRL
Package: airux-drivers
Version: $VERSION
Section: admin
Priority: optional
Architecture: all
Maintainer: AIRUX <airux@localhost>
Depends: python3 (>= 3.11), pkexec | policykit-1, chromium, curl, libnotify-bin, rfkill
Recommends: nvidia-detect, fwupd, mokutil, pciutils, usbutils
Installed-Size: $(du -sk "$STAGE" --exclude=DEBIAN | cut -f1)
Homepage: https://github.com/Absurd28/AIRUX_OS
Description: AIRUX Drivers: hardware status, firmware and driver installs
 Finds out which hardware works and why a part does not (missing firmware, switched off, no
 driver, a better vendor driver), and fixes it: firmware packages, NVIDIA's driver with Secure Boot
 key enrollment, rfkill, device firmware updates (fwupd). Includes a hardware report.
CTRL
deb="$OUT/airux-drivers_${VERSION}_all.deb"
dpkg-deb --build -Zxz "$STAGE" "$deb" >/dev/null
ls -la "$deb"
