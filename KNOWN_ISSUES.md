# AIRUX 0.1: known issues

What is known to be missing, weak or untested in 0.1. Evaluation numbers are in docs/EVAL.md.
The phase each issue was found in is in brackets.

## Not built or cut

- **No "Hey AIRA" wake word.** Push-to-talk (Talk button or Ctrl+Space) replaces it until a
  wake-word model is trained. [5]
- **No NVIDIA driver in the ISO.** It is CPU-only by choice: no non-free driver, and a smaller
  image. On an NVIDIA machine the model runs on the CPU with `nouveau` for display. Installing
  `nvidia-driver` afterwards and restarting `aira-llm` lets the profiler use the GPU, but this path
  is untested. [8]
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
- **Untested on real hardware so far:** the Plasma desktop, AIROX Code and the fixed audio stack.
  The last real-laptop test (with Xfce) failed; see docs/DEMO.md.

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
  Wi-Fi firmware, laptop audio and microphones, suspend, screen lock. [9: see docs/DEMO.md]

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
