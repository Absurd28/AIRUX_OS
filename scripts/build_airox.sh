#!/usr/bin/env bash
# Build airox-code_<version>_amd64.deb: VSCodium (the MIT-licensed open-source build of VS Code),
# rebranded as AIROX Code, with AIRA as a built-in extension. Output: $AIRUX_WORK/build/deb.
# Run as root (the Chromium sandbox helper must be setuid root inside the package).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${AIRUX_WORK:-/srv/airux-work}"
VSCODIUM_VERSION="1.143.17232"
VSCODIUM_SHA256="988330df203f4d2dbfb9ee0052a0356a27af7c0a2a7ff606424c531ae4a7b538"
VERSION="${VSCODIUM_VERSION}+airux1"
SRC_DEB="$WORK/downloads/codium_${VSCODIUM_VERSION}_amd64.deb"
OUT="$WORK/build/deb"
STAGE="$OUT/airox-code"
APP="$STAGE/usr/share/airox-code"
EXT="$REPO/airox-code/extension/aira"

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
mkdir -p "$WORK/downloads" "$OUT"

echo "== VSCodium $VSCODIUM_VERSION (pinned, checked by SHA-256)"
if [ ! -f "$SRC_DEB" ]; then
    curl -fsSL -o "$SRC_DEB.part" \
        "https://github.com/VSCodium/vscodium/releases/download/$VSCODIUM_VERSION/codium_${VSCODIUM_VERSION}_amd64.deb"
    mv "$SRC_DEB.part" "$SRC_DEB"
fi
echo "$VSCODIUM_SHA256  $SRC_DEB" | sha256sum -c -

echo "== unpack and move to /usr/share/airox-code"
rm -rf "$STAGE"
mkdir -p "$STAGE"
dpkg-deb -x "$SRC_DEB" "$STAGE"
mv "$STAGE/usr/share/codium" "$APP"
rm -rf "$STAGE/usr/share/applications" "$STAGE/usr/share/pixmaps" "$STAGE/usr/share/appdata" \
       "$STAGE/usr/share/bash-completion" "$STAGE/usr/share/zsh" "$STAGE/usr/share/mime" "$STAGE/usr/bin"

echo "== AIRA extension: unit tests on AIROX Code's own runtime"
ELECTRON_RUN_AS_NODE=1 "$APP/codium" --test "$EXT"/test/*.test.js

echo "== rebrand: product.json"
python3 - "$APP/resources/app/product.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d.update({
    "nameShort": "AIROX Code",
    "nameLong": "AIROX Code",
    "dataFolderName": ".airox-code",
    "urlProtocol": "airox-code",
    "linuxIconName": "airox-code",
    "reportIssueUrl": "https://github.com/Absurd28/AIRUX_OS/issues/new",
})
for k in ("updateUrl", "downloadUrl", "releaseNotesUrl"):   # AIROX Code updates come from AIRUX packages
    d.pop(k, None)
json.dump(d, open(p, "w"), indent="\t")
print("product:", d["nameLong"], "| extensions from", d.get("extensionsGallery", {}).get("serviceUrl"))
PY

echo "== AIRA as a built-in extension"
dest="$APP/resources/app/extensions/aira"
rm -rf "$dest"
mkdir -p "$dest"
cp -a "$EXT/package.json" "$EXT/extension.js" "$EXT/src" "$EXT/media" "$dest/"
rsvg-convert -w 128 -h 128 "$REPO/packaging/branding/aira-icon.svg" -o "$dest/media/aira.png"
cp "$REPO/LICENSE" "$dest/LICENSE"

echo "== icons, launcher, desktop entries"
for s in 16 32 48 64 128 256 512; do
    install -d "$STAGE/usr/share/icons/hicolor/${s}x${s}/apps"
    rsvg-convert -w $s -h $s "$REPO/airox-code/branding/airox-code.svg" -o "$STAGE/usr/share/icons/hicolor/${s}x${s}/apps/airox-code.png"
done
install -Dm644 "$REPO/airox-code/branding/airox-code.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/airox-code.svg"
cp "$STAGE/usr/share/icons/hicolor/512x512/apps/airox-code.png" "$APP/resources/app/resources/linux/code.png"
install -d "$STAGE/usr/bin"
ln -s ../share/airox-code/bin/codium "$STAGE/usr/bin/airox-code"
install -d "$STAGE/usr/share/applications"
cat > "$STAGE/usr/share/applications/airox-code.desktop" <<'DESK'
[Desktop Entry]
Name=AIROX Code
GenericName=Code Editor
Comment=Code editor with AIRA built in
Exec=/usr/share/airox-code/codium %F
Icon=airox-code
Type=Application
StartupNotify=true
StartupWMClass=codium
Categories=Development;IDE;TextEditor;
MimeType=text/plain;text/x-python;text/x-csrc;text/x-c++src;text/x-java;application/javascript;application/json;text/markdown;text/x-shellscript;inode/directory;
Actions=new-empty-window;
Keywords=code;editor;ide;vscode;programming;aira;

[Desktop Action new-empty-window]
Name=New Empty Window
Exec=/usr/share/airox-code/codium --new-window %F
Icon=airox-code
DESK
cat > "$STAGE/usr/share/applications/airox-code-url-handler.desktop" <<'DESK'
[Desktop Entry]
Name=AIROX Code - URL Handler
Exec=/usr/share/airox-code/codium --open-url %U
Icon=airox-code
Type=Application
NoDisplay=true
MimeType=x-scheme-handler/airox-code;
DESK

echo "== licences"
install -d "$STAGE/usr/share/doc/airox-code"
cp "$APP/resources/app/LICENSE.txt" "$STAGE/usr/share/doc/airox-code/LICENSE.vscodium.txt" 2>/dev/null || true
cp "$APP/resources/app/ThirdPartyNotices.txt" "$STAGE/usr/share/doc/airox-code/" 2>/dev/null || true
cat > "$STAGE/usr/share/doc/airox-code/README" <<'DOC'
AIROX Code is VSCodium (https://vscodium.com, MIT licence; built from Microsoft's open-source
VS Code repository) rebranded for AIRUX, with the AIRA extension (MIT) built in. AIRA talks only
to AIRA's model on this computer. Extensions come from Open VSX when you open the Extensions view.
DOC

echo "== package"
chown -R root:root "$STAGE"
chmod 4755 "$APP/chrome-sandbox"
install -d "$STAGE/DEBIAN"
depends=$(dpkg-deb -f "$SRC_DEB" Depends)
cat > "$STAGE/DEBIAN/control" <<CTRL
Package: airox-code
Version: $VERSION
Section: devel
Priority: optional
Architecture: amd64
Maintainer: AIRUX <airux@localhost>
Depends: $depends
Recommends: aira, git
Installed-Size: $(du -sk "$STAGE" --exclude=DEBIAN | cut -f1)
Homepage: https://github.com/Absurd28/AIRUX_OS
Description: AIROX Code, the code editor of AIRUX, with AIRA built in
 VSCodium (the open-source build of VS Code) rebranded for AIRUX, with AIRA as a built-in
 extension: chat about your code with cited sources, inline suggestions, edits applied only after
 review, commit messages and error explanations, all on AIRA's local model.
CTRL
cat > "$STAGE/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
if [ "$1" = configure ]; then
    update-alternatives --install /usr/bin/editor editor /usr/bin/airox-code 0 || true
    command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true
    command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q /usr/share/icons/hicolor || true
fi
exit 0
POST
cat > "$STAGE/DEBIAN/prerm" <<'PRERM'
#!/bin/sh
set -e
if [ "$1" = remove ]; then update-alternatives --remove editor /usr/bin/airox-code || true; fi
exit 0
PRERM
chmod 755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm"
deb="$OUT/airox-code_${VERSION}_amd64.deb"
dpkg-deb --build -Zzstd "$STAGE" "$deb" >/dev/null
ls -la "$deb"
dpkg-deb -f "$deb" Package Version Installed-Size
