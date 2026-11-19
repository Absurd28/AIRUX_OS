#!/usr/bin/env bash
# Build neox_<version>_all.deb: NEOX, the AIRUX browser (Chromium's engine with NEOX's profile,
# extension, Shields and privacy policies). Output: $AIRUX_WORK/build/deb. Run as root.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${AIRUX_WORK:-/srv/airux-work}"
VERSION="0.1.0"
UBOL_VERSION="2026.1116.1650"
UBOL_SHA256="67e084b34470c51c08fa9314b118b8cb73df3a72e90c6a3b536d041c92e61bbc"
UBOL_ZIP="$WORK/downloads/uBOLite_${UBOL_VERSION}.chromium.zip"
OUT="$WORK/build/deb"
STAGE="$OUT/neox"
N="$REPO/neox"
ELECTRON_NODE="$WORK/build/deb/airox-code/usr/share/airox-code/codium"   # Node to syntax-check the JS

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
mkdir -p "$WORK/downloads" "$OUT"

echo "== uBlock Origin Lite $UBOL_VERSION (Shields; pinned, checked by SHA-256)"
if [ ! -f "$UBOL_ZIP" ]; then
    curl -fsSL -o "$UBOL_ZIP.part" \
        "https://github.com/uBlockOrigin/uBOL-home/releases/download/$UBOL_VERSION/uBOLite_${UBOL_VERSION}.chromium.zip"
    mv "$UBOL_ZIP.part" "$UBOL_ZIP"
fi
echo "$UBOL_SHA256  $UBOL_ZIP" | sha256sum -c -

rm -rf "$STAGE"
mkdir -p "$STAGE/usr/share/neox" "$STAGE/usr/bin" "$STAGE/usr/share/applications" \
         "$STAGE/etc/chromium/policies/managed" "$STAGE/etc/chromium/native-messaging-hosts" "$STAGE/usr/share/doc/neox"
python3 -c "import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$UBOL_ZIP" "$STAGE/usr/share/neox/ubol"
python3 -c "import json,sys; m=json.load(open(sys.argv[1])); assert m['manifest_version'] == 3, m['manifest_version']" \
    "$STAGE/usr/share/neox/ubol/manifest.json"
cp "$STAGE/usr/share/neox/ubol/LICENSE.txt" "$STAGE/usr/share/doc/neox/LICENSE.uBOLite.txt" 2>/dev/null || true

echo "== NEOX extension"
cp -a "$N/extension" "$STAGE/usr/share/neox/extension"
ext="$STAGE/usr/share/neox/extension"
mkdir -p "$ext/icons"
for s in 16 32 48 128; do
    rsvg-convert -w $s -h $s "$N/branding/neox.svg" -o "$ext/icons/neox-$s.png"
done
for s in 16 32; do
    rsvg-convert -w $s -h $s "$REPO/packaging/branding/aira-icon.svg" -o "$ext/icons/aira-$s.png"
done
python3 - "$ext/manifest.json" <<'PY'
# the extension id the native host allows must be the one the manifest's key produces
import base64, hashlib, json, sys
key = base64.b64decode(json.load(open(sys.argv[1]))["key"])
ext_id = "".join(chr(ord("a") + int(c, 16)) for c in hashlib.sha256(key).hexdigest()[:32])
assert ext_id == "caeflmcomdjlfjeinfhpookmojmhiphd", ext_id
print("extension id", ext_id)
PY
if [ -x "$ELECTRON_NODE" ]; then
    for f in "$ext"/*.js "$ext"/content/*.js; do ELECTRON_RUN_AS_NODE=1 "$ELECTRON_NODE" --check "$f"; done
    echo "javascript syntax ok"
fi
for f in "$ext"/manifest.json "$ext"/rules/*.json "$N"/policies/*.json "$N"/native/*.json; do
    python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$f"
done

echo "== launchers, menu entries, icons, policies, native host"
install -m755 "$N/bin/neox" "$N/bin/neox-private" "$STAGE/usr/bin/"
install -Dm755 "$N/lib/user-extensions" "$STAGE/usr/lib/neox/user-extensions"
# a content stamp: each user's copies of the extensions are refreshed when it changes
( cd "$STAGE/usr/share/neox" && echo "$VERSION $(find extension ubol -type f -exec sha256sum {} + | sort | sha256sum | cut -c1-16)" )     > "$STAGE/usr/share/neox/VERSION"
cat "$STAGE/usr/share/neox/VERSION"
install -m644 "$N/applications/chromium.desktop" "$N/applications/neox-private.desktop" "$STAGE/usr/share/applications/"
for s in 16 24 32 48 64 128 256 512; do
    install -d "$STAGE/usr/share/icons/hicolor/${s}x${s}/apps"
    rsvg-convert -w $s -h $s "$N/branding/neox.svg" -o "$STAGE/usr/share/icons/hicolor/${s}x${s}/apps/neox.png"
done
install -Dm644 "$N/branding/neox.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/neox.svg"
install -m644 "$N/policies/neox.json" "$STAGE/etc/chromium/policies/managed/neox.json"
install -m644 "$N/native/org.airux.neox.json" "$STAGE/etc/chromium/native-messaging-hosts/org.airux.neox.json"
cat > "$STAGE/usr/share/doc/neox/README" <<'DOC'
NEOX is the AIRUX web browser: Debian's Chromium engine with NEOX's own profile, new tab page,
AIRA side panel and privacy settings. Shields are uBlock Origin Lite (GPL-3.0,
https://github.com/uBlockOrigin/uBOL-home). Private windows with Tor use Debian's tor package.
DOC

echo "== package"
chown -R root:root "$STAGE"
install -d "$STAGE/DEBIAN"
install -m755 "$N/debian/preinst" "$N/debian/postinst" "$N/debian/postrm" "$STAGE/DEBIAN/"
cat > "$STAGE/DEBIAN/control" <<CTRL
Package: neox
Version: $VERSION
Section: web
Priority: optional
Architecture: all
Maintainer: AIRUX <airux@localhost>
Depends: chromium (>= 120), tor, aira, libnotify-bin
Installed-Size: $(du -sk "$STAGE" --exclude=DEBIAN | cut -f1)
Homepage: https://github.com/Absurd28/AIRUX_OS
Description: NEOX, the AIRUX web browser, with AIRA built in
 Chromium's engine with NEOX's own name, icon, theme, new tab page and private profile; Shields
 (uBlock Origin Lite); strict privacy defaults (HTTPS-only, third-party cookies blocked, no Google
 sync or telemetry, private search, Global Privacy Control); partial fingerprinting protection;
 private windows with Tor; and AIRA as a side panel who can also browse for you in NEOX.
CTRL
deb="$OUT/neox_${VERSION}_all.deb"
dpkg-deb --build -Zxz "$STAGE" "$deb" >/dev/null
ls -la "$deb"
