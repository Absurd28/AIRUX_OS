# AIRUX 0.1: known issues

What is known to be missing, weak or untested in 0.1. Evaluation numbers are in docs/EVAL.md.
The phase each issue was found in is in brackets.

## Not built or cut

- **No "Hey AIRA" wake word.** Push-to-talk (Talk button or Ctrl+Space) replaces it. CLAUDE.md
  allows push-to-talk until a wake-word model is trained. [5]
- **No NVIDIA driver in the ISO.** It is CPU-only by choice: no non-free driver, and a smaller
  image. On an NVIDIA machine the model runs on the CPU with `nouveau` for display. Installing
  `nvidia-driver` afterwards and restarting `aira-llm` lets the profiler use the GPU, but this path
  is untested. [8]
- **Only the 2B model ships in the ISO.** Bigger tiers are downloaded after the first boot
  (`aira-models`, asks first). [8]
- Nothing from the CLAUDE.md cut list was cut: the load controller, quorum, browser control and
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
- **Small talk can offer things that do not apply.** For example, it offered a GPU check on a VM
  with no GPU. The reply checker stops invented facts and "done" claims, not irrelevant offers.
  [8, 9]

## Installer and image

- **Installs are UEFI-only.** grub-pc conflicts with grub-efi-amd64 in the offline package pool.
  Legacy BIOS can boot the live system but cannot install it. [8]
- **`hwclock` is missing** (util-linux-extra), so Calamares cannot set the hardware clock. The
  install still completes. [8]
- **Untested on real hardware so far:** Secure Boot (the stick boots through Debian's signed shim),
  Wi-Fi firmware, laptop audio and microphones, suspend, screen lock. [9: see docs/DEMO.md]

## Speed

- **CPU-only replies are slow.** In a 4-core VM with the 2B model, one spoken command took 27–48 s.
  [8]
- **Evaluation:** 2.5% errors on 200 commands (9B, development machine), with no unconfirmed risky
  calls and no unverified "done". About 1.5% after the last fix (not re-run in full).
  See docs/EVAL.md. [9]
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
