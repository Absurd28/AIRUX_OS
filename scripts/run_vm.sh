#!/bin/bash
# shellcheck disable=SC2054  # the commas inside QEMU arguments are QEMU's own option syntax
# Run AIRUX in a window on this PC: QEMU with KVM inside WSL2, shown through WSLg.
#
#   scripts/run_vm.sh live      try the live system (nothing is kept when you close it)
#   scripts/run_vm.sh install   live system plus a 40 GB virtual disk: use "Install AIRUX" on the desktop
#   scripts/run_vm.sh           start the installed system from that disk
#
# Settings (environment):
#   AIRUX_VM_MEM   MB of RAM for the VM (default 5120; WSL has 9 GB and Windows needs the rest)
#   AIRUX_VM_CPUS  virtual CPUs (default 6 of this laptop's 12)
#   AIRUX_VM_GPU   1 (default): the VM draws with the PC's GPU (virgl -> Mesa d3d12 in WSLg);
#                  0: software drawing, slow but the most compatible (use it if the window stays black)
#   AIRUX_VM_UI    gtk window backend: x11 (default) or wayland
#   AIRUX_ISO, AIRUX_VM (folder for the disk and the log)
set -euo pipefail
WORK=${AIRUX_WORK:-/srv/airux-work}
ISO=${AIRUX_ISO:-$WORK/build/iso/airux-0.1-amd64.hybrid.iso}
VM=${AIRUX_VM:-$WORK/myvm}
MEM=${AIRUX_VM_MEM:-5120}
CPUS=${AIRUX_VM_CPUS:-6}
GPU=${AIRUX_VM_GPU:-1}
UI=${AIRUX_VM_UI:-x11}
mode=${1:-run}

if [ ! -r /dev/kvm ] || [ ! -w /dev/kvm ]; then
  echo "No access to /dev/kvm. Run once:  sudo usermod -aG kvm $USER"
  echo "then in Windows:  wsl --terminate Debian   and open Debian again."
  exit 1
fi
# the VM's memory comes out of WSL's (and so Windows'): say so when it will be tight
avail=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
if [ "$avail" -lt $((MEM + 1024)) ]; then
  echo "Warning: WSL has only $avail MB available for a $MEM MB VM; AIRUX will be slow."
  echo "Close other programs or set a smaller AIRUX_VM_MEM (4096 still runs AIRA's 2B model)."
fi
mkdir -p "$VM"
[ -f "$VM/vars.fd" ] || cp /usr/share/OVMF/OVMF_VARS_4M.fd "$VM/vars.fd"   # UEFI settings of this VM

if [ "$GPU" = 1 ]; then
  gfx=(-device virtio-vga-gl,xres=1280,yres=800 -display gtk,gl=on,zoom-to-fit=on)
  export GALLIUM_DRIVER=d3d12        # WSLg's GPU path; without it Mesa falls back to CPU drawing
else
  gfx=(-device virtio-vga,xres=1280,yres=800 -display gtk,zoom-to-fit=on)
fi

# cache=none: the VM caches its own disk; without it WSL keeps a second copy, taken from Windows' RAM
args=(-enable-kvm -machine q35 -cpu host -m "$MEM" -smp "$CPUS" -name AIRUX
  -drive "if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd"
  -drive "if=pflash,format=raw,file=$VM/vars.fd"
  -netdev user,id=n0 -device virtio-net-pci,netdev=n0
  -device qemu-xhci -device usb-tablet -device usb-kbd
  -vga none "${gfx[@]}"
  -audiodev pa,id=snd0,server=unix:/mnt/wslg/PulseServer
  -device intel-hda -device hda-duplex,audiodev=snd0
  -device virtio-balloon-pci,free-page-reporting=on
  # guest agent socket, so scripts/qemu_ctl.py --dir $VM exec ... can look inside when debugging
  -chardev "socket,path=$VM/qga.sock,server=on,wait=off,id=qga0"
  -device virtio-serial -device virtserialport,chardev=qga0,name=org.qemu.guest_agent.0)
cdrom=(-drive "file=$ISO,media=cdrom,readonly=on,cache=none")
disk=(-drive "file=$VM/airux.qcow2,if=virtio,format=qcow2,cache=none,aio=threads")

case $mode in
  live)
    [ -f "$ISO" ] || { echo "No ISO at $ISO"; exit 1; }
    args+=("${cdrom[@]}") ;;
  install)
    [ -f "$ISO" ] || { echo "No ISO at $ISO"; exit 1; }
    [ -f "$VM/airux.qcow2" ] || qemu-img create -f qcow2 "$VM/airux.qcow2" 40G
    args+=("${disk[@]}" "${cdrom[@]}") ;;
  run)
    [ -f "$VM/airux.qcow2" ] || { echo "Nothing installed yet: run  $0 install  first."; exit 1; }
    args+=("${disk[@]}") ;;
  *) echo "usage: $0 [live|install|run]"; exit 2 ;;
esac

export DISPLAY=${DISPLAY:-:0} WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-wayland-0} GDK_BACKEND=$UI
echo "Starting AIRUX ($mode, $MEM MB, $CPUS CPUs, GPU drawing: $([ "$GPU" = 1 ] && echo on || echo off)); log: $VM/qemu.log"
exec qemu-system-x86_64 "${args[@]}" 2> "$VM/qemu.log"
