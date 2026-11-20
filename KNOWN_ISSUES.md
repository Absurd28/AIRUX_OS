# AIRUX 0.1: known issues

What is known to be missing, weak or untested in 0.1. Evaluation numbers are in docs/EVAL.md.
The phase each issue was found in is in brackets.

## Not built or cut

- **No "Hey AIRA" wake word.** Push-to-talk (Talk button or Ctrl+Space) replaces it until a
  wake-word model is trained. [5]
- **NVIDIA's driver is not preinstalled.** AIRUX Drivers installs it in one click (online: about
  300 MB, built for the running kernel with DKMS); with Secure Boot on, its signing key must be
  enrolled once at the next boot (blue MOK screen). Until then the model runs on the CPU and the
  display on `nouveau`. Tested only as a simulated install in QEMU (no NVIDIA GPU there).
- **Only the 2B model ships in the ISO.** Bigger tiers are downloaded after the first boot
  (`aira-models`, asks first). [8]
- Nothing from the planned cut list was cut: the load controller, quorum, browser control and
  research checker are all built.

## Safety and privacy

- **The polkit rule trusts the `aira` group, not only AIRA.** Any program run by a member of that
  group can start, stop and restart system units and install packages through PackageKit without
  a password. AIRA's own permission broker still asks first. A stricter rule would match AIRA's
  own helper. [8]
- **Without an unlocked keyring, the vault key is a file.** It lives in `~/.config/aira/vault.key`
  with mode 0600. That protects a copied vault file, not a readable home folder. With autologin
  and in the live session the keyring is locked, so the file is used. [4, 8]
- **Grounding layer 8 is lexical.** It catches wrong numbers and unrelated claims, but can pass a
  subtle embellishment that reuses the source's words. [6]
- **An ambiguous command can get a social reply.** "Delete it." got "Anytime." instead of "Delete
  what?" Nothing runs and nothing is claimed, but the router sometimes reads a bare command as
  thanks. Recorded in the evaluation (docs/EVAL.md, case 173). [9]
- **The "done without read-back" rule (layer 6) can blank a harmless reply.** It replaces the whole
  reply, so a recall answer that says something was "saved" can become "I'm not sure. I can't
  confirm that anything changed." This was seen once for "What do you know about me?" in phase 5.
  It is safe, but unhelpful. A finer rule would drop only the claiming sentence. [9]
- **Results vary between runs.** Risky calls are sampled three times at temperature 0.7 (layer
  5), so the same command can pass in one run and miss in the next. Phase 5 passed 8/10, then
  10/10 on the same code. [9]
- **On the 2B model, "Delete demo.txt in my home folder" usually stops at a question.** In QEMU it
  wrote `~/.demo.txt` in 3 of 4 dry runs. The entity check (layer 2) stopped it and asked "Did you
  mean ~/demo.txt?"; answering "yes" runs the request again with that path, through every check.
  On WSL with a GPU, its three samples disagreed, so AIRA asked instead. The 9B model asked to
  confirm deleting `~/demo.txt` in 4 of 4 runs. Nothing was ever deleted without confirmation. [9]
- **Small talk can offer things that do not apply.** For example, it offered a GPU check on a VM
  with no GPU. The reply checker stops invented facts and "done" claims, not irrelevant offers.
  [8, 9]

## Desktop and AIROX Code

- **8 GB of RAM is now the minimum.** Plasma, AIRA's side panel and the 2B model need about 4 GB
  together; under 6 GB, AIRA uses the lighter Piper voice.
- **AIRA's side panel is placed by a KWin script.** It recognises AIRA by its title and Chromium's
  app id. If you move or resize it, it stays where you put it until AIRA starts again.
- **The Start menu header shows a placeholder user picture** (Debian's), until you set your own in
  Settings → Users.
- **Drives have no letters (C:, D:).** Linux has one folder tree; Dolphin lists drives by name
  under Devices.
- **AIROX Code's extensions come from Open VSX,** like any VSCodium. Microsoft-only extensions
  (Pylance, C# Dev Kit, Remote SSH) are not available there. Opening the Extensions view contacts
  open-vsx.org.
- **AIRA in AIROX Code runs on the same local model as AIRA.** Inline suggestions take about 2 s
  on the 9B model with a GPU, and longer on the CPU-only 2B tier.
- **The live session never locks or blanks the screen** (on mains power). The live user's password
  is not known to anyone, so Plasma's 5-minute lock locked people out of the live system (and
  blocked Ctrl+Space). Installed systems keep the normal lock.
- **AIRA works in AIROX Code's Restricted Mode** (folders you have not trusted): she only reads files
  and proposes edits you accept, so her extension says it supports untrusted folders. Without that,
  VS Code turned her off there, and with her the Midnight Glass look.
- **AIRA Home replaces the Welcome page** a moment after AIROX Code starts (the AIRA extension
  starts after the window has opened), so Welcome can flash briefly.
- **Untested on real hardware so far:** the Plasma desktop, AIROX Code, NEOX and the fixed audio
  stack. The last real-laptop test (with Xfce) failed; see docs/DEMO.md.

## NEOX browser

- **NEOX is Chromium with AIRUX's layer, not a fork.** It is Debian's Chromium package with NEOX's
  own profile, extensions and policies, so some of Chromium's own screens and window titles still
  say "Chromium" (for example "New tab - Chromium").
- **Shields is uBlock Origin Lite (Manifest V3).** Debian's full uBlock Origin is a Manifest V2
  extension that Chromium 156 no longer loads. Lite blocks by filter lists only: no element
  picker and no custom dynamic filtering.
- **Safe Browsing is off** (it sends lookups to Google). NEOX relies on HTTPS-only mode and Shields.
- **Fingerprinting protection is partial:** canvas and audio readings get a small per-site, per-day
  noise, `navigator.hardwareConcurrency` reads 4, and Global Privacy Control is sent. WebGL, fonts
  and screen size are not changed, unlike Brave.
- **Saved passwords use Chromium's "basic" store** (not KDE Wallet): with the wallet, Chromium
  popped up "KDE Wallet Service" at start and held back every page until it was answered. The
  basic store is obfuscated, not encrypted.
- **The Tor window is not Tor Browser.** It sends traffic through Tor and leaves no profile behind,
  but it does not have Tor Browser's anti-fingerprinting. Tor starts only when a private window is
  opened (it is off at boot), and the window appears once Tor can carry traffic ("Connecting to
  the Tor network…"; on the live system this can take from seconds to a few minutes), and starting it needs the desktop session (the `aira` group's polkit
  rule above).
- **AIRA asks before opening web pages** (sending anything off the machine needs your OK), so
  "open the browser and search for X" shows a confirmation in AIRA's panel first.

- **AIRA's side panel in NEOX opens with Ctrl+Shift+Y** or the AIRA button pinned in the toolbar.
  Chromium keeps Ctrl+Shift+A for its own tab search, and Alt+Shift+A did not reach NEOX in testing.

## NEOX VPN

- **Free means someone else's servers.** RiseupVPN is run by volunteers on donations: speeds vary,
  and its places are only what Riseup offers (Paris, Amsterdam, Montreal and Miami in November
  2026; the list changes). Some Riseup exit IPs are geolocated to another country than the gateway's city
  (Amsterdam's often show as France), so websites may see a different country from the one picked.
- **Tor is slow and some sites block it.** Connecting takes up to a minute; exits in small
  countries may be few or none (connect then fails with Tor's reason).
- **WireGuard needs your own provider.** NEOX VPN does not create accounts; it imports `.conf`
  files you download from a provider (for example Proton VPN's free plan). Configs with
  PostUp/PostDown/PreUp/PreDown are refused because they would run commands as root. WireGuard
  hopping rotates through your imported configs. Tested in WSL against a local peer, not against a
  commercial provider.
- **Whole-computer mode has no kill switch.** If the VPN drops, other apps use the normal
  connection until you reconnect. Only-NEOX mode does block (NEOX holds its pages back).
- **Whole-computer Tor carries TCP and DNS only.** Other UDP and all IPv6 are blocked while it is
  on (no leaks), so UDP apps such as games, some video calls and NTP time sync stop working until
  you disconnect. LAN addresses stay direct.
- **Changing place briefly interrupts Riseup and WireGuard** (a few seconds while the new tunnel
  comes up; NEOX's pages wait in only-NEOX mode). Tor changes exit without a gap; open connections
  keep their old exit until they close.
- **Only-NEOX mode applies to NEOX's normal windows**, not its private Tor window (which already
  uses Tor), and its SOCKS proxy at 10.218.0.2 can be used by any program on this computer.
- **"Check my IP" asks ifconfig.co** (through the VPN when it is on), and each new Riseup tunnel
  is tested with a TCP handshake to Quad9 (9.9.9.9) or Cloudflare (1.1.1.1) on port 443 through
  the tunnel; Riseup gateways that accept a connection but reach nothing are skipped this way.

## Phagos (antivirus)

- **Its own signatures only.** Phagos has 13 in-house rules and a blocklist, not a database of
  millions of known samples: it catches the families its rules and immune engines describe, and
  misses most specific known malware that a commercial or ClamAV database would name. This was the
  chosen trade-off (fully in-house).
- **Real-time checks fail open.** A program waits at most 1 s for its verdict; if a scan takes
  longer, the program runs and is scanned right after. If phagosd stops, the kernel lets
  everything run.
- **Behaviour monitoring samples every 2 s,** so a fast ransomware gets a few seconds of work
  before it is paused: in the QEMU check a simulator rewriting about 50 files a second was paused
  after 136 of its 400 test files (about 3 s). Decoy files stop it at once when it reaches one.
- **Under a flood of writes, some files are not scanned when written.** Phagos keeps at most 4,000
  written files waiting for a scan, and skips the rest (in a test with 60,000 writes, about 10,000
  were skipped). They are still checked when someone runs them. This keeps program starts fast:
  holding every write event once exhausted the open files and froze all program starts in a VM.
- **On the 2B model, AIRA's answers about Phagos are often the raw tool output** ("Here is what
  security returned: ...") because her own wording fails the reply checker; once she ran "status"
  when asked to scan. Bigger tiers word it properly.
- **Negative Selection alone never blocks.** An unusual program (a new build, a tool you compiled)
  is only noted in the history unless the heuristics or signatures agree.
- **Your own scripts can be paused** if they rewrite many files with random-looking data quickly
  (for example batch-encrypting backups). Choose "Allow" once; Phagos remembers the script.
- **Packaged programs are checked on their first 256 KiB and last 64 KiB when they start,** not
  hashed in full; replaced system programs are found by the weekly integrity check (dpkg --verify).

## Games

- **AIRUX Snake runs in a Chromium app window,** so it uses more memory than a native game (a
  Chromium renderer). Its best scores are kept per mode and speed in its own small profile.
- **Knights shows its new-game dialog first;** you against Stockfish (white) is preselected.
  Closing Knights mid-game asks whether to save it.
- **The KDE games are Debian's packages, unchanged,** so they follow the desktop's Breeze theme
  rather than AIRUX's colours.

## Drivers and firmware

- **AIRUX does not write drivers; it ships firmware and installs vendor drivers.** Linux's drivers
  are in the kernel. Since this version the ISO includes Debian's firmware for Intel, MediaTek,
  Realtek, Atheros and Broadcom Wi-Fi and Bluetooth, AMD/Intel/NVIDIA graphics, Intel SOF laptop
  audio and CPU microcode (about 0.4 GB). Earlier ISOs shipped none, which is why Wi-Fi, Bluetooth
  and the microphone failed on a real laptop.
- **Not included:** firmware-qcom-soc (ARM phones and boards), legacy NVIDIA drivers (Kepler and
  older keep `nouveau`), and Broadcom's proprietary `wl` driver (needs a module build; most
  Broadcom chips work with `brcmfmac`).
- **Newer devices than Debian 13** can need newer firmware: AIRUX Drivers offers it from Debian
  backports when the installed package lacks the file the kernel asked for.
- **Intel IPU (MIPI) laptop cameras do not work** with Debian 13's kernel; AIRUX Drivers says so.
- **In the live session, installed drivers last until shutdown.** Install AIRUX to keep them.
- **Device firmware updates (fwupd)** need the internet and a device that LVFS supports.
- **On the 2B model, AIRA's hardware answers often come out as "I'm not sure"**: she reads the
  diagnosis, but her wording trips the "no 'done' without read-back" rule. The AIRUX Drivers app
  and `airux-drivers status` show the same facts directly. Bigger tiers answer properly.

## Installer and image

- **Installs are UEFI-only.** grub-pc conflicts with grub-efi-amd64 in the offline package pool.
  Legacy BIOS can boot the live system but cannot install it. [8]
- **`hwclock` is missing** (util-linux-extra), so Calamares cannot set the hardware clock. The
  install still completes. [8]
- **Fixed in phase 9 after the QEMU demo dry run:**
  - Reminders never fired on Xfce: the user services waited for graphical-session.target, which
    Xfce never starts. They are now started by XDG autostart.
  - The live account's name "AIRUX" was used as the user's name.
- **Untested on real hardware so far:** Secure Boot (the stick boots through Debian's signed shim),
  the new firmware set, laptop audio and microphones, suspend, screen lock. [9: see docs/DEMO.md]

## Speed

- **CPU-only replies are slow.** In a 4-core VM with the 2B model, one spoken command took 27–48 s.
  [8]
- **Evaluation:** 2.5% errors on 200 commands (9B, development machine), with no unconfirmed risky
  calls. The only flagged "done" is a detector false positive. See docs/EVAL.md. [9]
- **The 9B model is slowed by slots on a 6 GB GPU.** The model and the parallel slots compete for
  VRAM, so speed is traded for slots. The median on the development machine (9B, 3 slots) is
  12 s per command. [1, 9]

## Voice

- **The voice tests used synthetic speech** (Kokoro voices), not a real person at a real
  microphone. [5, 8]
- "AIRA" is sometimes transcribed as "Ira" (harmless so far). [5]
- **Kokoro's phonemizer is GPL-3.0.** eSpeak NG is loaded at runtime; see LICENSES.md. [5]

## Tools

- **The web search uses DuckDuckGo,** which sometimes answers with a bot check. AIRA reports it
  plainly. [6]
- On the development machine (WSL) there is no clipboard tool, no Podman and no desktop. Those
  tools fail there by design, and the evaluation counts how AIRA reports the failures. [2, 9]
