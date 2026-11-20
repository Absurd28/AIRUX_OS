#!/usr/bin/env bash
# Build phagos_<version>_all.deb: Phagos, AIRUX's own antivirus (immune-system design). Output:
# $AIRUX_WORK/build/deb. Run as root. Standard library only: Debian's python3 is all it needs.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${AIRUX_WORK:-/srv/airux-work}"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$REPO/phagos/__init__.py")"
OUT="$WORK/build/deb"
STAGE="$OUT/phagos"
P="$REPO/phagos"
DIST="$P/packaging"

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
mkdir -p "$OUT"
rm -rf "$STAGE"

echo "== checks: every module compiles, the rules load"
python3 -m py_compile "$P"/*.py "$P"/engines/*.py
( cd "$REPO" && python3 -c "from phagos.engines.rules import RuleSet; r = RuleSet.load('phagos/rules'); print(len(r.rules), 'rules,', len(r.blocklist), 'blocklisted')" )

echo "== files"
install -d "$STAGE/usr/lib/phagos" "$STAGE/usr/share/phagos" "$STAGE/usr/bin" "$STAGE/usr/sbin" \
    "$STAGE/usr/lib/systemd/system" "$STAGE/usr/share/polkit-1/actions" "$STAGE/usr/share/applications" \
    "$STAGE/etc/xdg/autostart" "$STAGE/usr/share/doc/phagos"
( cd "$REPO" && tar --exclude=__pycache__ --exclude=packaging --exclude='*.pyc' -cf - phagos ) | tar -xf - -C "$STAGE/usr/lib/phagos"
cp -a "$P/rules" "$STAGE/usr/share/phagos/rules"
install -m755 "$DIST/bin/phagos" "$DIST/bin/phagos-security" "$STAGE/usr/bin/"
install -m755 "$DIST/bin/phagosd" "$STAGE/usr/sbin/"
install -m755 "$DIST/bin/phagos-agent" "$STAGE/usr/lib/phagos/"
install -m644 "$DIST"/systemd/* "$STAGE/usr/lib/systemd/system/"
install -m644 "$DIST/polkit/org.airux.phagos.policy" "$STAGE/usr/share/polkit-1/actions/"
install -m644 "$DIST/applications/phagos-security.desktop" "$STAGE/usr/share/applications/"
install -m644 "$DIST/applications/phagos-agent.desktop" "$STAGE/etc/xdg/autostart/"
install -Dm644 "$P/ui/phagos.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/phagos.svg"
for s in 16 22 24 32 48 64 128 256; do
    install -d "$STAGE/usr/share/icons/hicolor/${s}x${s}/apps"
    rsvg-convert -w $s -h $s "$P/ui/phagos.svg" -o "$STAGE/usr/share/icons/hicolor/${s}x${s}/apps/phagos.png"
done
cat > "$STAGE/usr/share/doc/phagos/README" <<'DOC'
Phagos is AIRUX's own antivirus, designed like an immune system:
  signatures      in-house rules (/usr/share/phagos/rules, local ones in /etc/phagos/rules.d)
  heuristics      static traits of programs and scripts
  Negative Selection  detectors for what is foreign to this computer's own programs
  Clonal Selection    detectors that caught a threat multiply and adapt to its variants
  Dendritic Cells     Danger Theory: programs judged by the damage they do (ransomware, miners)
Real-time protection uses fanotify; phagosd runs as root (systemctl status phagosd).
Commands: phagos status | scan | history | quarantine | allowed; app: Phagos Security.
Licence: MIT (part of AIRUX).
DOC

echo "== package"
chown -R root:root "$STAGE"
install -d "$STAGE/DEBIAN"
install -m755 "$DIST/debian/postinst" "$DIST/debian/prerm" "$DIST/debian/postrm" "$STAGE/DEBIAN/"
cat > "$STAGE/DEBIAN/control" <<CTRL
Package: phagos
Version: $VERSION
Section: admin
Priority: optional
Architecture: all
Maintainer: AIRUX <airux@localhost>
Depends: python3 (>= 3.11), libnotify-bin, polkitd | policykit-1, chromium, curl
Installed-Size: $(du -sk "$STAGE" --exclude=DEBIAN | cut -f1)
Homepage: https://github.com/Absurd28/AIRUX_OS
Description: Phagos, the AIRUX antivirus (immune-system design)
 Real-time protection (fanotify), quick/full/custom scans, quarantine, protection history and a
 Windows Security-style app. Detection is in-house: signature rules, heuristics, Negative Selection
 and Clonal Selection (artificial immune system) and the Dendritic Cell Algorithm (Danger Theory)
 for behaviour such as ransomware.
CTRL
deb="$OUT/phagos_${VERSION}_all.deb"
dpkg-deb --build -Zxz "$STAGE" "$deb" >/dev/null
ls -la "$deb"
