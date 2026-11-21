#!/usr/bin/env bash
# Build airox-connect_<version>_all.deb: AIROX Connect, the PC side (airox-connectd, the window,
# "Send to phone", the desktop helper) plus the Android app's APK to share from the PC.
# Output: $AIRUX_WORK/build/deb. Run as root.
# The APK comes from the app's own repository (github.com/Absurd28/AIROX_Connect, checked out next to
# this one as ../AIROX_Connect; gradlew assembleDebug), or from $AIROX_APK.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${AIRUX_WORK:-/srv/airux-work}"
D="$REPO/connect"
P="$D/packaging"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$D/airox_connect/__init__.py")"
OUT="$WORK/build/deb"
STAGE="$OUT/airox-connect"
APK="${AIROX_APK:-$REPO/../AIROX_Connect/app/build/outputs/apk/debug/app-debug.apk}"

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
[ -f "$APK" ] || { echo "no APK at $APK (build it first: ../AIROX_Connect, gradlew assembleDebug)" >&2; exit 1; }
mkdir -p "$OUT"
rm -rf "$STAGE"

echo "== checks: every module compiles"
python3 -m py_compile "$D"/airox_connect/*.py

echo "== files"
install -d "$STAGE/usr/lib/airox-connect" "$STAGE/usr/bin" "$STAGE/usr/share/applications" "$STAGE/etc/xdg/autostart" \
    "$STAGE/usr/share/kio/servicemenus" "$STAGE/usr/share/airox-connect" "$STAGE/usr/share/doc/airox-connect"
( cd "$D" && tar --exclude=__pycache__ --exclude='*.pyc' -cf - airox_connect ) | tar -xf - -C "$STAGE/usr/lib/airox-connect"
install -m755 "$P/bin/airox-connectd" "$P/bin/airox-connect" "$P/bin/airox-connect-send" "$STAGE/usr/bin/"
install -m644 "$P/applications/airox-connect.desktop" "$STAGE/usr/share/applications/"
install -m644 "$P/autostart/airox-connectd.desktop" "$STAGE/etc/xdg/autostart/"
install -m644 "$P/servicemenus/airox-connect-send.desktop" "$STAGE/usr/share/kio/servicemenus/"
install -m644 "$APK" "$STAGE/usr/share/airox-connect/airox-connect.apk"
install -Dm644 "$D/airox_connect/ui/airox-connect.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/airox-connect.svg"
for s in 16 22 24 32 48 64 128 256; do
    install -d "$STAGE/usr/share/icons/hicolor/${s}x${s}/apps"
    rsvg-convert -w $s -h $s "$D/airox_connect/ui/airox-connect.svg" -o "$STAGE/usr/share/icons/hicolor/${s}x${s}/apps/airox-connect.png"
done
install -m644 "$REPO/docs/CONNECT_PROTOCOL.md" "$STAGE/usr/share/doc/airox-connect/"
cat > "$STAGE/usr/share/doc/airox-connect/README" <<'DOC'
AIROX Connect lets a paired Android phone or tablet on the same network talk to AIRA (typed or
spoken), see running apps and AIRA's activity, use the Task Manager, send files and media both ways,
share the clipboard and links, act as touchpad, keyboard and media remote, glance at the screen,
become a second screen, and lock, sleep or wake the PC.
It is off until you turn on "Allow phone connections" in the AIROX Connect window. Phones pair by
QR code (you accept on the PC), talk TLS 1.3 to this PC's own pinned certificate with a per-device
token, only from the local network. Every action passes AIRA's permission broker (risky ones are
confirmed on the phone) and is logged in AIRA's action log. Protocol: CONNECT_PROTOCOL.md.
The Android app is in /usr/share/airox-connect/airox-connect.apk ("Share the app" in the window).
Licence: MIT (part of AIRUX); the app's libraries are listed in LICENSES.md of AIRUX.
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
Package: airox-connect
Version: $VERSION
Section: net
Priority: optional
Architecture: all
Maintainer: AIRUX <airux@localhost>
Depends: aira, airux-taskmgr, python3 (>= 3.11), python3-dbus, python3-gi, gir1.2-glib-2.0, openssl, iproute2,
 chromium, curl, libnotify-bin, qrencode, wl-clipboard, playerctl, wireplumber, kde-spectacle, libkscreen-bin,
 xdg-desktop-portal-kde, gstreamer1.0-tools, gstreamer1.0-pipewire, gstreamer1.0-plugins-base,
 gstreamer1.0-plugins-good, gstreamer1.0-plugins-bad, gstreamer1.0-plugins-ugly
Recommends: gstreamer1.0-vaapi
Installed-Size: $(du -sk "$STAGE" --exclude=DEBIAN | cut -f1)
Homepage: https://github.com/Absurd28/AIRUX_OS
Description: AIROX Connect: use AIRA and this PC from your Android phone
 Pair a phone or tablet by QR code and, on the same network: talk to AIRA by text or voice, see
 running apps and AIRA's activity, use the Task Manager, send files and media both ways, share the
 clipboard and links, use the phone as touchpad and media remote, glance at the screen, use it as a
 second screen, and lock, sleep or wake the PC. Every action passes AIRA's permission broker.
CTRL
deb="$OUT/airox-connect_${VERSION}_all.deb"
dpkg-deb --build -Zxz "$STAGE" "$deb" >/dev/null
ls -la "$deb"
