#!/usr/bin/env bash
# Build the AIRUX 0.1 ISO with live-build into $AIRUX_WORK/build/iso. Run as root on Debian 13,
# after scripts/build_deb.sh. The package cache in build/iso/cache is kept between builds.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${AIRUX_WORK:-/srv/airux-work}"
B="$WORK/build/iso"
BR="$REPO/packaging/branding"

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
mkdir -p "$B"
cd "$B"
if [ -d config ]; then lb clean noauto >/dev/null 2>&1 || true; fi
rm -rf auto config
cp -a "$REPO/image/live-build/." "$B/"
chmod 755 "$B"/config/includes.chroot/usr/lib/live/config/*   # live-config components (a Windows checkout loses +x)

echo "== branding assets"
inc="$B/config/includes.chroot"
mkdir -p "$inc/usr/share/backgrounds/airux" "$inc/usr/share/plymouth/themes/airux" \
         "$inc/usr/share/icons/hicolor/256x256/apps"
rsvg-convert -w 1920 -h 1080 "$BR/airux-wallpaper.svg" -o "$inc/usr/share/backgrounds/airux/airux-wallpaper.png"
rsvg-convert -w 1920 -h 1080 "$BR/airux-splash.svg" -o "$inc/usr/share/plymouth/themes/airux/splash.png"
rsvg-convert -w 18 -h 18 "$BR/plymouth/airux/dot.svg" -o "$inc/usr/share/plymouth/themes/airux/dot.png"
cp "$BR/plymouth/airux/airux.plymouth" "$BR/plymouth/airux/airux.script" "$inc/usr/share/plymouth/themes/airux/"
cal="$inc/etc/calamares/branding/airux"
rsvg-convert -w 128 -h 128 "$BR/airux-logo.svg" -o "$cal/airux-logo.png"
# the default user picture (login, lock screen, Start menu header): the AIRUX logo, not Debian's swirl
mkdir -p "$inc/etc/skel"
rsvg-convert -w 256 -h 256 "$BR/airux-logo.svg" -o "$inc/etc/skel/.face.icon"
rsvg-convert -w 640 -h 360 "$BR/airux-splash.svg" -o "$cal/welcome.png"

echo "== boot menus: AIRUX splash and title, 5 s timeout (live-build's defaults wait forever)"
mkdir -p "$B/config/bootloaders"
cp -a /usr/share/live/build/bootloaders/grub-pc /usr/share/live/build/bootloaders/syslinux "$B/config/bootloaders/"
cp "$BR/airux-bootmenu.svg" "$B/config/bootloaders/splash.svg"
sed -i 's/^set default=0$/set default=0\nset timeout=5/' "$B/config/bootloaders/grub-pc/config.cfg"
sed -i 's/^title-text:.*/title-text: "AIRUX 0.1"/' "$B/config/bootloaders/grub-pc/live-theme/theme.txt"
sed -i 's/^timeout 0$/timeout 50/' "$B/config/bootloaders/syslinux/syslinux.cfg"
grep -q "set timeout=5" "$B/config/bootloaders/grub-pc/config.cfg"
grep -q "timeout 50" "$B/config/bootloaders/syslinux/syslinux.cfg"

echo "== AIRA packages, AIROX Code, NEOX, Phagos and AIRUX Drivers"
mkdir -p "$B/config/packages.chroot"
cp "$WORK"/build/deb/aira_*_amd64.deb "$WORK"/build/deb/aira-models-base_*_all.deb \
   "$WORK"/build/deb/airox-code_*_amd64.deb "$WORK"/build/deb/neox_*_all.deb \
   "$WORK"/build/deb/phagos_*_all.deb "$WORK"/build/deb/airux-drivers_*_all.deb "$B/config/packages.chroot/"

echo "== lb config and build (logs in $B/build.log)"
lb config
lb build
ls -la "$B"/*.iso
sha256sum "$B"/*.iso | tee "$B/SHA256SUMS"
