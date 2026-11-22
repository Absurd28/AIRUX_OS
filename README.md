<div align="center">

<img src="docs/assets/airux-banner.svg" alt="AIRUX" width="100%"/>

<h3>A Debian-based operating system with <b>AIRA</b>, a fully local voice assistant that controls your system safely and won't make things up</h3>

[![License: MIT](https://img.shields.io/badge/License-MIT-59c38a.svg?style=for-the-badge)](LICENSE)
[![Debian 13](https://img.shields.io/badge/Debian-13%20trixie-A81D33?style=for-the-badge&logo=debian&logoColor=white)](https://www.debian.org/)
[![Python](https://img.shields.io/badge/Python-3.13-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![llama.cpp](https://img.shields.io/badge/LLM-llama.cpp%20%C2%B7%20Qwen3.5-7cc4ff?style=for-the-badge)](https://github.com/ggml-org/llama.cpp)

[![CI](https://github.com/Absurd28/AIRUX_OS/actions/workflows/ci.yml/badge.svg)](https://github.com/Absurd28/AIRUX_OS/actions/workflows/ci.yml)
[![Eval](https://img.shields.io/badge/eval%20error%20rate-2.5%25%20of%20200-59c38a?style=flat-square)](docs/EVAL.md)
[![Safety](https://img.shields.io/badge/unconfirmed%20risky%20actions-0-59c38a?style=flat-square)](docs/EVAL.md)
[![Offline](https://img.shields.io/badge/runs-100%25%20offline-7cc4ff?style=flat-square)](docs/ARCHITECTURE.md)

[Features](#-features) ·
[Screenshots](#-screenshots) ·
[Phone app](#-airox-connect-airux-on-your-phone) ·
[Installation](#-installation-guide) ·
[Build from source](#-build-from-source) ·
[Architecture](#-architecture) ·
[Evaluation](#-evaluation) ·
[Roadmap](#-roadmap)

</div>

---

## ✨ What is AIRUX?

**AIRUX** is an operating system for developers, engineers, researchers and scientists. It is
Debian 13 with a developer toolchain, plus **AIRA** (*Adaptive Intelligent Research Assistant*), a
voice assistant built into the desktop.

You talk to AIRA ("How much disk space do I have?", "Run the tests in my project", "Remind me in
ten minutes to stretch") and she does it with real tools on your machine. **Everything runs
locally:** the language model, speech recognition, text-to-speech and her encrypted memory. Nothing
leaves your computer unless you say so.

What makes AIRA different is the **grounding guard**. It is a set of checks written in code, not
prompts:
- She never acts on files, apps or services that don't exist.
- She never claims an action is "done" without checking the result.
- She confirms with you before anything risky.
- She says *"I'm not sure"* instead of guessing.

## 🚀 Features

| | |
| --- | --- |
| 🎙️ **Voice first** | Push-to-talk (Ctrl+Space). [whisper.cpp](https://github.com/ggml-org/whisper.cpp) understands you, and [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) gives AIRA a natural voice, with a Piper fallback for small machines. |
| 🧠 **One local model, five agents** | A Qwen3.5 GGUF served by `llama-server` powers an orchestrator and five specialists: **System, Desktop, Dev, Research and Assistant**. They run as parallel slots, not as copies of the model. |
| 📏 **Sized to your hardware** | At first boot a profiler measures RAM, VRAM, cores and tokens per second, then picks the model tier (2B, 4B, 9B or 27B) and the number of parallel agents. |
| 🛡️ **Permission broker** | Every tool call goes through it. Deleting, overwriting, installing, root actions, network changes and anything sent off the machine wait for your confirmation. This is enforced in code, and every action is logged. |
| ✅ **Grounding guard** | Nine layers: constrained tool calls, entity checks, facts only from tools, stop on failure, 2-of-3 agreement for risky actions, read-back after acting, a reply checker, citations, and honest abstention. |
| 🔐 **Encrypted memory** | A SQLCipher vault with keyword and vector search, retention rules, decay and a quota. Ask "what do you know about me", "forget X", "export my memory" or "pause memory". |
| ⏰ **Reminders and briefing** | A reminder daemon gives desktop notifications and speaks them aloud. A login briefing covers today's events, due reminders and watched jobs; calendars import from .ics files. |
| 🔬 **Research tools** | Search local notes and papers, summarise PDFs with citations, inspect datasets, run Jupyter notebooks, and search arXiv and the web (with your OK). |
| 🐝 **Worker swarm** | Async workers for file search and fetching, a "pheromone board" that learns which approach works, and a load controller that adds or sheds agents with system pressure. |
| 🪟 **Windows 11-style desktop** | KDE Plasma 6 laid out like Windows 11: a centred taskbar with a Start button, pinned apps and search, a File Explorer (Dolphin), a full Settings app (Wi-Fi, Bluetooth, display, sound, power, apps) and the shortcuts you know (Win, Win+E, Win+I, Win+D, Win+L, Win+arrows). |
| 🖥️ **AIRA side panel** | AIRA docks on the right like Copilot: live transcript, active agents, pending confirmations and verified/unverified tags. Ctrl+Space talks to her from anywhere, and **AIRA Settings** covers voice, names, memory and privacy. |
| 💻 **AIROX Code** | The AIRUX code editor: VSCodium (the open-source build of VS Code) with AIRA built in, like Copilot, and a look of its own (*Midnight Glass*: floating rounded panels, pill tabs, a top activity bar and an **AIRA Home** start page). It offers chat with cited sources, inline suggestions, edits applied only after review, commit messages and error explanations, all on the local model. |
| 🌐 **NEOX browser** | The AIRUX browser, built on Chromium's engine with Brave-style protection: **Shields** (uBlock Origin Lite), strict privacy defaults (HTTPS-only, third-party cookies blocked, no telemetry, DuckDuckGo), **Global Privacy Control**, fingerprinting protection, a **private window with Tor**, and **NEOX VPN**: a free VPN (RiseupVPN, Tor, or your own WireGuard) for NEOX alone or the whole computer, chosen when you connect, that can change country every *x* minutes. AIRA browses in it: say *"open the browser and search for X"*, or ask about the page you're reading in NEOX's AIRA side panel, where answers must quote the page. |
| 🦠 **Phagos antivirus** | AIRUX's own antivirus, designed like an immune system: real-time protection (files are checked as they are written, and malicious programs are not allowed to start), in-house signature rules, **Negative Selection** and **Clonal Selection** (artificial immune system) and the **Dendritic Cell Algorithm** from Danger Theory, which pauses a program that starts encrypting your files and lets you decide. Quarantine, scans, protection history and a Windows Security-style app; ask AIRA "is my computer protected?" |
| 🧩 **AIRUX Drivers** | Like Windows' Device Manager: shows whether Wi-Fi, Ethernet, Bluetooth, graphics, sound, camera, touchpad and battery work, and why a part does not (missing firmware, switched off, no driver). One click installs the fix, including **NVIDIA's driver** with the Secure Boot key step. It also does device firmware updates (fwupd) and saves a hardware report. The ISO ships Debian's firmware for Wi-Fi, Bluetooth, GPUs and laptop audio. Ask AIRA "why isn't my Wi-Fi working?" |
| 📊 **Task Manager** | AIRUX's own Task Manager, like Windows 11's (**Ctrl+Shift+Esc**): apps, background and system processes with CPU, memory, disk and GPU use; live performance graphs for CPU, memory, disks, network and GPU; startup apps; users; details; services. Ending a program or stopping a service asks first and is logged, like every AIRA action. |
| 📱 **[AIROX Connect](https://github.com/Absurd28/AIROX_Connect)** | An Android app for your phone or tablet, paired with the PC by QR code, on the same network: talk to AIRA by text or voice (she answers on the phone in her own voice), see running apps and what AIRA is doing, use the Task Manager, send files and media both ways, share the clipboard and links, use the phone as touchpad, keyboard and media remote, glance at the screen, make the phone or tablet a **second screen** with touch, lock, sleep or wake the PC, and get AIRA's reminders and Phagos alerts as notifications. Encrypted to your PC's own certificate, and every action passes AIRA's permission broker (risky ones are approved on the phone). |
| 🎮 **Offline games** | Chess against Stockfish (Knights), Sudoku, Solitaire, Mahjongg, Minesweeper, Reversi, Four in a Row, Gomoku, nonograms, falling blocks, Breakout and more from KDE, plus **AIRUX Snake**, our own take on the classic snake game. All of them work without internet. |
| 💿 **Real OS image** | A live ISO with AIRUX branding, a Plymouth splash and the Calamares installer. It is tested end to end in QEMU: boot, install, then AIRA answering by voice. |

## 📸 Screenshots

Every screenshot is the live ISO running in QEMU, taken by `tests/screenshot_tour.py`: AIRA's answers
come from the real local model (the 2B tier, on 4 CPU cores), and pages are real pages.

<div align="center">

| Desktop with AIRA docked | Start menu |
| :---: | :---: |
| <img src="docs/screenshots/tour-01-desktop.png" width="420"/> | <img src="docs/screenshots/tour-02-start-menu.png" width="420"/> |
| **AIRA answering from a tool, not a guess** | **AIROX Code: AIRA finds the bug (her line number was off, so her answer is tagged unverified)** |
| <img src="docs/screenshots/tour-03-aira-answers.png" width="420"/> | <img src="docs/screenshots/tour-04-airox-code-aira.png" width="420"/> |
| **NEOX: AIRA summarizes the page, quoting it** | **"Open the browser and search for…": AIRA drives NEOX** |
| <img src="docs/screenshots/tour-05-neox-aira-panel.png" width="420"/> | <img src="docs/screenshots/tour-06-aira-browses-neox.png" width="420"/> |
| **Chess against Stockfish (Knights)** | **Sudoku (KSudoku)** |
| <img src="docs/screenshots/tour-07-chess.png" width="420"/> | <img src="docs/screenshots/tour-08-sudoku.png" width="420"/> |
| **AIRUX Snake** | **File Explorer** |
| <img src="docs/screenshots/tour-09-snake.png" width="420"/> | <img src="docs/screenshots/tour-10-files.png" width="420"/> |
| **About this System** | **The installer** |
| <img src="docs/screenshots/tour-11-about.png" width="420"/> | <img src="docs/screenshots/tour-12-installer.png" width="420"/> |
| **AIRUX Drivers** | **Phagos pauses a ransomware-like program** |
| <img src="docs/screenshots/drivers-app.png" width="420"/> | <img src="docs/screenshots/phagos-3-ransomware-paused.png" width="420"/> |
| **Phagos Security** | **Task Manager (Ctrl+Shift+Esc)** |
| <img src="docs/screenshots/phagos-5-app.png" width="420"/> | <img src="docs/screenshots/task-manager.png" width="420"/> |
| **Installer finished** | **Installed system** |
| <img src="docs/screenshots/vm-install-done.png" width="420"/> | <img src="docs/screenshots/vm-installed-desktop.png" width="420"/> |

</div>

## 📱 AIROX Connect: AIRUX on your phone

**[AIROX Connect](https://github.com/Absurd28/AIROX_Connect)** is the Android app for AIRUX, in its own
repository. Pair your phone or tablet with the PC by QR code, then talk to AIRA (she answers in her
own voice), watch and manage what the PC is running, send files both ways, use the phone as a
touchpad, or drag a window onto it as a **second screen**. It works on your own network with no
cloud, the connection is encrypted to your PC's own certificate, and every action goes through
AIRA's permission broker on the PC.

<div align="center">

| Home | Talk to AIRA | Task Manager | Risky actions ask first |
| :---: | :---: | :---: | :---: |
| <img src="docs/screenshots/connect-home.png" width="200"/> | <img src="docs/screenshots/connect-aira.png" width="200"/> | <img src="docs/screenshots/connect-tasks.png" width="200"/> | <img src="docs/screenshots/connect-approval.png" width="200"/> |

**Second screen: the PC's Task Manager, dragged onto the phone**

<img src="docs/screenshots/connect-second-screen.png" width="760"/>

</div>

The PC side (`airox-connectd` and the **AIROX Connect** window) ships in the ISO; the app is in
[Absurd28/AIROX_Connect](https://github.com/Absurd28/AIROX_Connect). Setup is
[Step 6](#step-6-connect-your-android-phone-optional) of the installation guide.

## 💻 System requirements

| | Minimum | Recommended |
| --- | --- | --- |
| CPU | 64-bit x86 (amd64), 4 cores | 6+ cores |
| Memory | 8 GB RAM (2B model) | 16 GB RAM, or a GPU with 6 GB or more (9B model) |
| Disk | 25 GB for an install | 40 GB or more |
| Firmware | UEFI (Secure Boot can stay on) | UEFI |
| Audio | speakers or headphones | plus a microphone for voice |

The model tier is picked automatically from total memory (RAM plus VRAM):

| Total memory | Under 6 GB | 6–8 GB | 8–24 GB | 24 GB of GPU or more |
| --- | --- | --- | --- | --- |
| Model | Qwen3.5 2B | Qwen3.5 4B | Qwen3.5 9B | Qwen3.5 27B |

## 📦 Installation guide

> **Download AIRUX 0.1** from the website, [airux-os.pages.dev](https://airux-os.pages.dev/download/)
> (the 4.66 GB ISO is hosted on [SourceForge](https://sourceforge.net/projects/airux/files/0.1/),
> as GitHub limits release files to 2 GB). Or build it yourself with
> [Build from source](#-build-from-source).

### Step 1: Check the ISO

Download `airux-0.1-amd64.hybrid.iso` and `SHA256SUMS` into the same folder (a build writes both),
then check that they match.

```bash
sha256sum -c SHA256SUMS            # Linux
```

```powershell
Get-FileHash .\airux-0.1-amd64.hybrid.iso -Algorithm SHA256   # Windows
```

### Step 2: Write it to a USB stick (8 GB or larger; it will be erased)

- **Windows:** use [Rufus](https://rufus.ie). Select the stick and the ISO, click **Start**, and
  choose **DD Image mode** when asked.
- **Windows or macOS:** use [balenaEtcher](https://etcher.balena.io): *Flash from file*, then the
  stick, then *Flash*.
- **Linux:** find the stick with `lsblk`, then run (double-check `/dev/sdX`):

  ```bash
  sudo dd if=airux-0.1-amd64.hybrid.iso of=/dev/sdX bs=4M status=progress conv=fsync
  ```

### Step 3: Boot from the stick

1. Plug the stick in and power on while pressing the boot-menu key:

   | Brand | Key |
   | --- | --- |
   | Acer | F12 |
   | Dell | F12 |
   | HP | F9 |
   | Lenovo | F12 |
   | ASUS | Esc |

   On some laptops (Acer, for example) the boot menu is off by default: enable it in firmware
   setup first (F2).
2. Choose the USB stick (UEFI). The AIRUX menu starts the live system after 5 seconds.
3. The live desktop logs in automatically, and AIRA's shell opens. The model services take about
   1–3 minutes to load; AIRA is ready when her status shows **idle**.

### Step 4: Talk to AIRA

- Press **Ctrl+Space** (or click **Talk**), speak, then press it again. You can also type in the
  box at the bottom.
- Try:
  - *"How much free disk space do I have in my home folder?"*
  - *"Create a file called notes.txt that says hello."*
  - *"Remind me in 2 minutes to stretch."*
- Risky actions appear under **Waiting for you**, and nothing happens until you confirm.

### Step 5: Install to disk (optional)

> ⚠️ **"Erase disk" wipes the whole disk, including any other operating system.** Use a spare
> disk or the *Manual* partitioning option if you want to keep Windows or another OS.

1. Double-click **Install AIRUX** on the desktop.
2. Choose your language, location and keyboard. Then choose the partitioning, and your name, user
   name and password.
3. Click **Install**, wait for *"All done"*, restart and remove the stick.
4. After logging in, AIRA starts full screen. If your hardware can run a bigger model, AIRA offers
   to download it (`aira-models`), asking you first.

The full guide, with troubleshooting (no boot, black screen, no sound, no microphone), is in
**[docs/USB.md](docs/USB.md)**. A five-command demo for new hardware is in
**[docs/DEMO.md](docs/DEMO.md)**.

### Step 6: Connect your Android phone (optional)

The phone app is **AIROX Connect**: source, screenshots and build steps are at
**[github.com/Absurd28/AIROX_Connect](https://github.com/Absurd28/AIROX_Connect)**.

1. On the PC, open **AIROX Connect** from the Start menu and turn on **Allow phone connections**.
2. Get the app (Android 10 or newer), either way:
   - click **Share the app** and scan the code with the phone's camera, or copy
     `/usr/share/airox-connect/airox-connect.apk` to the phone;
   - or build it from the [AIROX Connect repository](https://github.com/Absurd28/AIROX_Connect)
     (`./gradlew assembleDebug`).

   Open the APK and allow installing from that source.
3. In the app tap **Scan the pairing code**, then click **Show pairing code** on the PC and point the
   phone at it. Click **Allow** on the PC.
4. The phone and the PC must be on the same network. For the second screen and the touchpad, KDE asks
   on the PC once ("Screen Sharing": choose **New Virtual Output**; "Remote control": **Share**).

### Option B: Add AIRA to an existing Debian 13 system

If you already run Debian 13 (trixie), build the two packages ([Build from source](#-build-from-source),
steps 1–5), then:

```bash
sudo apt install ./aira_0.1.0_amd64.deb ./aira-models-base_0.1.0_all.deb
```

This installs AIRA's model services (`aira-llm`, `aira-embed`, `aira-stt`), the reminder daemon,
the login briefing and the AIRA side panel (autostarted at login). Log out and back in, then run
`aira-shell`, or use `aira` in a terminal.

## 🛠️ Build from source

Everything builds on **Debian 13 (trixie)**: a real machine, a VM, or WSL2 on Windows. Large files
go to `$AIRUX_WORK` (default `/srv/airux-work`).

**1. Install the build tools**

```bash
sudo apt install git build-essential cmake python3 python3-venv python3-dev curl \
                 live-build librsvg2-bin
# optional, to test the image in a VM:
sudo apt install qemu-system-x86 qemu-utils ovmf
```

**2. Get the code and a work folder**

```bash
git clone https://github.com/Absurd28/AIRUX_OS.git && cd AIRUX_OS
sudo mkdir -p /srv/airux-work && sudo chown "$USER" /srv/airux-work
```

**3. Build the inference engines** (pinned versions, checked against their commits)

```bash
bash scripts/setup_llama.sh                     # llama.cpp (CUDA if nvcc is installed)
AIRUX_CPU_ONLY=1 bash scripts/setup_llama.sh    # portable CPU build, shipped in the image
bash scripts/setup_whisper.sh                   # whisper.cpp
```

**4. Download and verify the models** (about 11 GB for every tier; each file is checked by SHA-256)

```bash
python3 scripts/fetch_models.py                       # everything in models/MANIFEST.toml
python3 scripts/fetch_models.py --only qwen3.5-2b     # or just what you need
```

**5. Set up Python and run the tests**

```bash
python3 -m venv /srv/airux-work/venv
/srv/airux-work/venv/bin/pip install -r requirements.lock -r requirements-dev.lock
/srv/airux-work/venv/bin/python -m pytest -q tests/unit        # 390 tests
scripts/ci_lint.sh        # line endings, shellcheck, Python/JSON/TOML, launchers (as CI runs them)
```

**6. Build the packages and the ISO** (as root)

```bash
sudo bash scripts/build_deb.sh      # -> /srv/airux-work/build/deb/*.deb
sudo bash scripts/build_iso.sh      # -> /srv/airux-work/build/iso/airux-0.1-amd64.hybrid.iso
```

**7. Try it in a window, or test the image in QEMU (optional)**

On Windows with WSL2, `scripts/run_vm.sh` runs the ISO in a window through WSLg, drawn with the PC's
GPU (virgl and Mesa's d3d12 driver); add your user to the `kvm` group once.

```bash
scripts/run_vm.sh live       # try it; nothing is kept
scripts/run_vm.sh install    # adds a 40 GB virtual disk for the installer
scripts/run_vm.sh            # start the installed system
```

The automated checks:

```bash
cp tests/phase8_vm.example.toml tests/phase8_vm.toml   # set a password for the test VM
sudo /srv/airux-work/venv/bin/python tests/phase8_check.py live
sudo /srv/airux-work/venv/bin/python tests/demo_vm.py          # the five-command voice demo
```

## 🧭 Architecture

```mermaid
flowchart LR
    U([You: voice or text]) --> STT[whisper.cpp]
    STT --> O[Orchestrator]
    O --> S[System]
    O --> D[Desktop]
    O --> V[Dev]
    O --> R[Research]
    O --> A[Assistant]
    S & D & V & R & A --> B{{Permission broker}}
    B -->|read-only| T[(Tools)]
    B -->|risky| C[You confirm] --> T
    T --> G[Grounding guard<br/>read-back · reply checker]
    G --> TTS[Kokoro voice] --> U
    A <--> M[(Encrypted<br/>memory vault)]
```

| Layer | Grounding rule (enforced in code) |
| --- | --- |
| 1 | Tool calls are constrained by a JSON-schema grammar |
| 2 | Every file, app, service, contact or event must exist before acting |
| 3 | Facts come only from tool results in the same turn |
| 4 | A failed lookup stops the turn; nothing is filled in |
| 5 | Risky actions are sampled 3 times; 2 must agree, and then you confirm |
| 6 | Nothing is "done" until the new state is read back |
| 7 | Every number, path, name, time and day word in a reply is checked against the tool output |
| 8 | Research answers cite sources; unsupported sentences are flagged |
| 9 | "I'm not sure" counts as a correct answer when evidence is missing |

The full design is in **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

## 📊 Evaluation

AIRA was evaluated on **200 new commands**: tool tasks, multi-agent tasks, risky actions, 60 traps
(missing files, unknowable facts, failing tools, ambiguous requests) and small talk. Every
confirmation was denied, so nothing risky could run.

| Metric | Result |
| --- | --- |
| Error rate | **2.5%** (5 / 200), down from 7.5% at the baseline |
| Risky actions run without confirmation | **0** |
| Actions on missing items (60 traps) | **0** |
| Unknowable questions answered with a guess | **0** |
| Voice demo in QEMU (live ISO, 2B model on CPU) | **5 / 5** |

The details are in [docs/EVAL.md](docs/EVAL.md), and the limitations are listed honestly in
[KNOWN_ISSUES.md](KNOWN_ISSUES.md).

## 🗂️ Project structure

```text
aira/            AIRA: orchestrator, agents, grounding guard, permission broker
  tools/         system · desktop · dev · research · assistant tools
  memory/        SQLCipher vault, embeddings, keys
  profiler/      hardware probe, tier and slot planning, server launch
  swarm/         worker pool, pheromone board, quorum, load controller
  shell/         AIRA side panel and AIRA Settings (HTTP + SSE, Chromium)
airox-code/      AIROX Code: the AIRA extension (chat, inline, reviewed edits, AIRA Home), Midnight Glass theme
drivers/         AIRUX Drivers: hardware inventory and diagnosis, root helper, app
phagos/          Phagos antivirus: engines (rules, NSA, CLONALG, DCA), daemon, agent, app, rules
neox/            NEOX browser: extension (AIRA bridge, side panel, new tab, privacy), launchers, policies
neox/vpn/        NEOX VPN: neox-vpnd (Riseup, Tor, WireGuard; only-NEOX namespace or whole computer; hopping)
taskmgr/         AIRUX Task Manager: processes, performance, startup apps, services; actions through the broker
connect/         AIROX Connect, PC side: airox-connectd (pairing, TLS, REST + WebSockets), desktop helper, window
                 (the Android app is its own repository: https://github.com/Absurd28/AIROX_Connect)
image/           live-build configuration for the AIRUX ISO
packaging/       systemd units, polkit rule, branding, Debian packaging
scripts/         build llama.cpp / whisper.cpp, fetch models, build .deb and ISO, QEMU control
models/          MANIFEST.toml: every model file with its source and SHA-256
tests/           unit tests, milestone checks, 200-command evaluation, VM tests
docs/            architecture, USB guide, demo, evaluation reports
```

## 🗺️ Roadmap

- [x] Local LLM with hardware-based tier and slot selection
- [x] Five agents, permission broker and a 9-layer grounding guard
- [x] Encrypted memory, reminders and login briefing
- [x] Offline voice: whisper.cpp in, Kokoro out
- [x] Worker swarm, research tools, browser control
- [x] Bootable, installable ISO tested in QEMU
- [x] 200-command evaluation
- [x] Windows 11-style Plasma desktop with AIRA docked like Copilot
- [x] AIROX Code: a VS Code-based editor with AIRA built in, with its own Midnight Glass look
- [x] NEOX: a Chromium-based private browser with Shields, Tor windows and AIRA built in
- [x] NEOX VPN: free VPN (Riseup, Tor, your WireGuard) for NEOX or the whole computer, with country hopping
- [x] Phagos: an in-house antivirus built on artificial immune systems and Danger Theory
- [x] AIRUX Drivers: firmware for real hardware, NVIDIA driver installs, hardware diagnosis
- [x] AIRUX Task Manager (Ctrl+Shift+Esc)
- [x] AIROX Connect for Android: AIRA, Task Manager, files, touchpad and second screen from the phone (same network)
- [ ] AIROX Connect from other networks (a Tor onion service: no account, no port forwarding)
- [ ] AIROX Connect for iOS
- [ ] Real-hardware validation across more laptops
- [ ] "Hey AIRA" wake word (openWakeWord)
- [ ] Optional NVIDIA driver in the image
- [ ] Legacy BIOS installs
- [x] Prebuilt ISO downloads ([the website](https://airux-os.pages.dev/))
- [x] CI on every push and pull request (unit tests and lint)

## 🤝 Contributing

Issues and pull requests are welcome. CI runs the unit tests and the lint checks on every pull
request; to run them yourself first:

```bash
/srv/airux-work/venv/bin/python -m pytest -q tests/unit
scripts/ci_lint.sh
```

New tools must declare their risks so the permission broker can protect users, and must come with
tests.

## 📄 License

AIRUX's own code is released under the **[MIT License](LICENSE)**. Bundled third-party components
(Debian, llama.cpp, whisper.cpp, Qwen3.5, Kokoro, Piper and others) keep their own licenses, listed
in **[LICENSES.md](LICENSES.md)**.

<div align="center">
<sub>Built by <a href="https://github.com/Absurd28">Aryan Mitra</a> · AIRUX 0.1</sub>
</div>
