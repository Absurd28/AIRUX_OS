#!/bin/bash
# Run AIRUX in a window on this PC: QEMU with KVM inside WSL2, shown through WSLg.
#
#   scripts/run_vm.sh live      try the live system (nothing is kept when you close it)
#   scripts/run_vm.sh install   live system plus a 40 GB virtual disk: use "Install AIRUX" on the desktop
#   scripts/run_vm.sh           start the installed system from that disk
#
# Settings (environment): AIRUX_ISO, AIRUX_VM (folder for the disk), AIRUX_VM_MEM (MB, default 5120;
# WSL has 9 GB), AIRUX_VM_CPUS (default 4).
set -euo pipefail
WORK=${AIRUX_WORK:-/srv/airux-work}
ISO=${AIRUX_ISO:-$WORK/build/iso/airux-0.1-amd64.hybrid.iso}
VM=${AIRUX_VM:-$WORK/myvm}
MEM=${AIRUX_VM_MEM:-5120}
CPUS=${AIRUX_VM_CPUS:-4}
mode=${1:-run}

if [ ! -r /dev/kvm ] || [ ! -w /dev/kvm ]; then
  echo "No access to /dev/kvm. Run once:  sudo usermod -aG kvm $USER"
  echo "then in Windows:  wsl --terminate Debian   and open Debian again."
  exit 1
fi
mkdir -p "$VM"
[ -f "$VM/vars.fd" ] || cp /usr/share/OVMF/OVMF_VARS_4M.fd "$VM/vars.fd"   # UEFI settings of this VM

args=(-enable-kvm -machine q35 -cpu host -m "$MEM" -smp "$CPUS" -name AIRUX
  -drive "if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd"
  -drive "if=pflash,format=raw,file=$VM/vars.fd"
  -netdev user,id=n0 -device virtio-net-pci,netdev=n0
  -device qemu-xhci -device usb-tablet -device usb-kbd
  -vga none -device virtio-vga,xres=1280,yres=800
  -display gtk,zoom-to-fit=on
  -audiodev pa,id=snd0,server=unix:/mnt/wslg/PulseServer
  -device intel-hda -device hda-duplex,audiodev=snd0
  -device virtio-balloon-pci,free-page-reporting=on)

case $mode in
  live)
    [ -f "$ISO" ] || { echo "No ISO at $ISO"; exit 1; }
    args+=(-cdrom "$ISO") ;;
  install)
    [ -f "$ISO" ] || { echo "No ISO at $ISO"; exit 1; }
    [ -f "$VM/airux.qcow2" ] || qemu-img create -f qcow2 "$VM/airux.qcow2" 40G
    args+=(-drive "file=$VM/airux.qcow2,if=virtio,format=qcow2" -cdrom "$ISO") ;;
  run)
    [ -f "$VM/airux.qcow2" ] || { echo "Nothing installed yet: run  $0 install  first."; exit 1; }
    args+=(-drive "file=$VM/airux.qcow2,if=virtio,format=qcow2") ;;
  *) echo "usage: $0 [live|install|run]"; exit 2 ;;
esac

export DISPLAY=${DISPLAY:-:0} GDK_BACKEND=x11
exec qemu-system-x86_64 "${args[@]}"
