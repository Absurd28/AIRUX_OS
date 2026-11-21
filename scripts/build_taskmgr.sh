#!/usr/bin/env bash
# Build airux-taskmgr_<version>_all.deb: AIRUX Task Manager (Ctrl+Shift+Esc). Output: $AIRUX_WORK/build/deb.
# Run as root. It runs in AIRA's runtime (/usr/lib/aira/run) so its actions pass AIRA's broker.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${AIRUX_WORK:-/srv/airux-work}"
D="$REPO/taskmgr"
P="$D/packaging"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$D/airux_taskmgr/__init__.py")"
OUT="$WORK/build/deb"
STAGE="$OUT/airux-taskmgr"

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
mkdir -p "$OUT"
rm -rf "$STAGE"

echo "== checks: every module compiles"
python3 -m py_compile "$D"/airux_taskmgr/*.py

echo "== files"
install -d "$STAGE/usr/lib/airux-taskmgr" "$STAGE/usr/bin" "$STAGE/usr/share/applications" "$STAGE/usr/share/doc/airux-taskmgr"
( cd "$D" && tar --exclude=__pycache__ --exclude='*.pyc' -cf - airux_taskmgr ) | tar -xf - -C "$STAGE/usr/lib/airux-taskmgr"
install -m755 "$P/bin/airux-taskmgr" "$STAGE/usr/bin/"
install -m644 "$P/applications/airux-taskmgr.desktop" "$STAGE/usr/share/applications/"
install -Dm644 "$D/airux_taskmgr/ui/airux-taskmgr.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/airux-taskmgr.svg"
for s in 16 22 24 32 48 64 128 256; do
    install -d "$STAGE/usr/share/icons/hicolor/${s}x${s}/apps"
    rsvg-convert -w $s -h $s "$D/airux_taskmgr/ui/airux-taskmgr.svg" -o "$STAGE/usr/share/icons/hicolor/${s}x${s}/apps/airux-taskmgr.png"
done
cat > "$STAGE/usr/share/doc/airux-taskmgr/README" <<'DOC'
AIRUX Task Manager (Ctrl+Shift+Esc), like Windows 11's: Processes grouped into Apps, Background and
System processes; Performance (CPU, memory, disks, network, GPU); Startup apps; Users; Details;
Services. Ending a program, stopping a service, raising a priority or changing your own autostart
file passes AIRA's permission broker: it asks first, and every action is logged in AIRA's action
log. The same backend serves AIROX Connect on your phone. Licence: MIT (part of AIRUX).
DOC

echo "== package"
chown -R root:root "$STAGE"
install -d "$STAGE/DEBIAN"
cat > "$STAGE/DEBIAN/postinst" <<'SH'
#!/bin/sh
set -e
if [ "$1" = configure ]; then
    command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q /usr/share/icons/hicolor || true
    command -v update-desktop-database >/dev/null && update-desktop-database -q || true
fi
exit 0
SH
chmod 755 "$STAGE/DEBIAN/postinst"
cat > "$STAGE/DEBIAN/control" <<CTRL
Package: airux-taskmgr
Version: $VERSION
Section: admin
Priority: optional
Architecture: all
Maintainer: AIRUX <airux@localhost>
Depends: aira, chromium, curl, libnotify-bin, pkexec | policykit-1, systemd
Recommends: dolphin
Installed-Size: $(du -sk "$STAGE" --exclude=DEBIAN | cut -f1)
Homepage: https://github.com/Absurd28/AIRUX_OS
Description: AIRUX Task Manager
 Processes (apps, background, system), live performance graphs for CPU, memory, disks, network and
 GPUs, startup apps, users and services, like the Windows 11 Task Manager. Actions pass AIRA's
 permission broker. Opens with Ctrl+Shift+Esc.
CTRL
deb="$OUT/airux-taskmgr_${VERSION}_all.deb"
dpkg-deb --build -Zxz "$STAGE" "$deb" >/dev/null
ls -la "$deb"
