# Running AIRUX 0.1 from a USB stick

## What you need

- A USB stick of 8 GB or more. **Everything on it is erased.**
- The ISO, `airux-0.1-amd64.hybrid.iso` (4.66 GB). It is built in WSL at
  `/srv/airux-work/build/iso/`; from Windows open
  `\\wsl.localhost\Debian\srv\airux-work\build\iso\` in File Explorer and copy the ISO and
  `SHA256SUMS` somewhere local (for example Downloads).
- A 64-bit PC with UEFI firmware. Secure Boot can stay on: the stick boots through Debian's
  Microsoft-signed `shim`. Legacy BIOS can boot the live system but cannot install it.
- 4 GB of RAM at least; 8 GB or more is better. The ISO is CPU-only and ships the 2B model.

## 1. Check the download

Windows PowerShell:

```powershell
Get-FileHash .\airux-0.1-amd64.hybrid.iso -Algorithm SHA256
```

Linux:

```bash
sha256sum -c SHA256SUMS
```

The hash must match the one in `SHA256SUMS`.

## 2. Write the stick

**Windows, Rufus** (rufus.ie): pick the stick and the ISO, then Start. When Rufus asks
how to write a hybrid image, choose **DD Image mode**; ISO mode does not boot this image
reliably.

**Windows, or macOS, with balenaEtcher**: Flash from file, choose the ISO, choose the stick, then Flash.

**Linux**: find the stick with `lsblk` (here `/dev/sdX`; make sure it is the stick), then:

```bash
sudo dd if=airux-0.1-amd64.hybrid.iso of=/dev/sdX bs=4M status=progress conv=fsync
```

## 3. Boot from it

1. Plug in the stick and power on, pressing the boot-menu key:

   | Brand | Key |
   | --- | --- |
   | Acer | F12 |
   | Dell | F12 |
   | HP | F9 |
   | Lenovo | F12 |
   | ASUS | Esc |

   On Acer laptops (including the Nitro ANV15-51) the F12 menu is off by default. Turn it on
   first: press F2 at power-on, open Main, and set **F12 Boot Menu** to Enabled.
2. Choose the USB stick (UEFI).
3. The AIRUX menu starts the live system after 5 seconds.
4. The live desktop logs in as `airux` (no password). AIRA's shell opens in a window. The three
   model services take about 1–3 minutes to load from the stick. AIRA's status shows "idle" when
   she is ready.

## 4. Talk to AIRA

- Click **Talk** or press **Ctrl+Space**, speak, then press it again. The "Hey AIRA" wake word is
  not built yet; push-to-talk replaces it.
- Typing in the box at the bottom works too.
- Risky actions (delete, overwrite, install, restart, anything sent off the machine) appear under
  **Waiting for you**. Nothing happens until you confirm.

## 5. Install (optional)

**The installer's "Erase disk" option wipes the whole disk, including Windows.** On a machine
you want to keep, use the live system only, or install on a spare disk or computer.

1. Double-click **Install AIRUX** on the desktop.
2. Choose language, location and keyboard, then the partitioning: Erase disk, or Manual for a
   spare partition. Then set your name, user name, computer name and password.
3. Install, then restart and remove the stick. The installed system starts AIRA full screen
   after login.

The first boot measures the hardware. If it finds room for a bigger model (4B or 9B), AIRA can
download it with `aira-models`; she asks you first.

## If something goes wrong

| Problem | What to do |
| --- | --- |
| The stick is not in the boot menu | Rewrite it in DD mode. Or turn off Secure Boot to test. |
| Black screen after the menu | At the AIRUX menu, press `e` and add `nomodeset` to the line that starts with `linux`. |
| AIRA says she can't reach the model | Wait a minute. Then check `systemctl status aira-llm` in a terminal. |
| No sound | Check the volume icon in the panel. Then run `pactl list short sinks` in a terminal. |
| No microphone input | Pick the input device under the volume icon. Then run `arecord -l`. |

On a machine with an NVIDIA GPU, the ISO uses the open-source `nouveau` driver and runs the model
on the CPU. NVIDIA's driver is not included in 0.1 (see KNOWN_ISSUES.md).
