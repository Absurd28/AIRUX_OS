# Licences of shipped third-party components

## Models and voices

| Component | Use | Licence | Source |
| --- | --- | --- | --- |
| Qwen3.5 2B / 4B / 9B (Q4_K_M GGUF, Unsloth builds) | Language model | Apache-2.0 | https://huggingface.co/unsloth |
| nomic-embed-text v1.5 (Q8_0 GGUF) | Memory vault embeddings | Apache-2.0 | https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF |
| Kokoro-82M v1.0 weights | AIRA's voice | Apache-2.0 | https://huggingface.co/hexgrad/Kokoro-82M |
| kokoro-onnx model export (kokoro-v1.0.onnx, voices-v1.0.bin) | ONNX build of Kokoro | MIT | https://github.com/thewh1teagle/kokoro-onnx |
| whisper.cpp models (ggml-base.en, ggml-small.en) | Speech to text | MIT | https://huggingface.co/ggerganov/whisper.cpp |

### Shipped voices

| Voice | Engine | Use | Licence |
| --- | --- | --- | --- |
| af_heart (default), af_bella, af_nicole, af_sarah, af_sky, af_nova, bf_emma, bf_isabella | Kokoro-82M | AIRA, switchable by voice | Apache-2.0 (Kokoro-82M voice packs) |
| en_US-ljspeech-medium | Piper | Fallback under 6 GB of memory | Voice model trained on the LJ Speech dataset, public domain (model card: rhasspy/piper-voices) |

Not shipped: en_US-amy (dataset licence only given as "See URL"). No voice is cloned from a real person.

## Software

| Component | Use | Licence | Source |
| --- | --- | --- | --- |
| llama.cpp v1.0.0 | Model server | MIT | https://github.com/ggml-org/llama.cpp |
| whisper.cpp v1.9.5 | Speech recognition server | MIT | https://github.com/ggml-org/whisper.cpp |
| Piper (piper-tts 1.8.0, piper1-gpl) | Fallback speech engine | GPL-3.0-or-later; run as a separate program, not imported by AIRA | https://github.com/OHF-voice/piper1-gpl |
| sqlcipher3 0.6.5 (SQLCipher 4.20.0 community) | Encrypted vault | zlib (sqlcipher3), BSD-3-Clause (SQLCipher) | https://github.com/coleifer/sqlcipher3 |
| sqlite-vec 0.1.9 | Vector search | MIT / Apache-2.0 | https://github.com/asg017/sqlite-vec |
| kokoro-onnx 0.6.1 | Kokoro runtime | MIT | https://github.com/thewh1teagle/kokoro-onnx |
| onnxruntime 1.32.0 | ONNX inference | MIT | https://github.com/microsoft/onnxruntime |
| espeakng-loader / eSpeak NG | Phonemes for Kokoro | GPL-3.0 (eSpeak NG data and library, loaded at runtime) | https://github.com/espeak-ng/espeak-ng |
| phonemizer 3.6.0 | Phonemes for Kokoro | GPL-3.0 | https://github.com/bootphon/phonemizer |

Note for packaging (phase 8): eSpeak NG and phonemizer are GPL-3.0 and are imported in-process by
kokoro-onnx. Before redistribution, decide AIRA's own licence with that in mind or isolate speech
synthesis in a separate process as is done for Piper.

## Games (offline)

| Component | Use | Licence | Source |
| --- | --- | --- | --- |
| AIRUX Snake | Snake game made for AIRUX (games/snake) | MIT, like the rest of AIRUX | this repository |
| Stockfish 17 | Chess engine Knights plays against | GPL-3.0 | Debian package `stockfish` |
| KDE games: Knights, KSudoku, KPatience, KMahjongg, KMines, KReversi, Four in a Row, Bovo, Picmi, KBlocks, KBreakout, KDiamond, KBounce, Naval Battle | Offline games | GPL-2.0-or-later / LGPL (see each package's copyright file) | Debian packages, unmodified |

## Security

| Component | Use | Licence | Source |
| --- | --- | --- | --- |
| Phagos (engines, rules, daemon, app) | AIRUX's antivirus | MIT, like the rest of AIRUX | this repository (phagos/) |

## Networking

| Component | Use | Licence | Source |
| --- | --- | --- | --- |
| NEOX VPN (neox-vpnd, VPN tab) | Free VPN in NEOX | MIT, like the rest of AIRUX | this repository (neox/vpn/) |
| OpenVPN, NetworkManager-OpenVPN | Riseup tunnels | GPL-2.0 | Debian packages `openvpn`, `network-manager-openvpn` |
| WireGuard tools | WireGuard tunnels | GPL-2.0 | Debian package `wireguard-tools` |
| Tor | Tor backend and private windows | BSD-3-Clause | Debian package `tor` |
| nftables | Tor whole-computer redirect | GPL-2.0 | Debian package `nftables` |
| RiseupVPN service | Free VPN gateways (used, not shipped) | Riseup's terms of service, https://riseup.net/en/vpn | riseup.net |

## AIROX Connect and Task Manager

| Component | Use | Licence | Source |
| --- | --- | --- | --- |
| AIROX Connect (airox-connectd, the PC window, the Android app) and AIRUX Task Manager | Phone link and task manager | MIT, like the rest of AIRUX | this repository (connect/, taskmgr/); the app: https://github.com/Absurd28/AIROX_Connect |
| AndroidX (Core, Activity, Fragment, Lifecycle, Biometric, CameraX), Jetpack Compose, Material 3, Material icons | The Android app's UI and platform libraries | Apache-2.0 | https://developer.android.com/jetpack/androidx |
| Kotlin standard library, kotlinx.coroutines | The Android app's language runtime | Apache-2.0 | https://kotlinlang.org |
| OkHttp 4.12 and Okio | HTTPS and WebSocket in the app | Apache-2.0 | https://square.github.io/okhttp/ |
| ZXing core 3.5.3 | Reading the pairing QR code in the app | Apache-2.0 | https://github.com/zxing/zxing |
| GStreamer (base, good, bad, ugly, PipeWire plugin), x264 | Second-screen video encoding, run as a separate program (`gst-launch-1.0`) | LGPL-2.1-or-later; x264 GPL-2.0-or-later | Debian packages `gstreamer1.0-*` |
| qrencode | The pairing QR code on the PC, run as a separate program | LGPL-2.1-or-later | Debian package `qrencode` |
| playerctl, wl-clipboard, python3-dbus, python3-gi | Media keys, clipboard, portal access | LGPL-3.0 / GPL-3.0 / MIT / LGPL-2.1 (each a separate program or Debian's Python bindings) | Debian packages |

## Firmware and drivers

| Component | Use | Licence | Source |
| --- | --- | --- | --- |
| AIRUX Drivers | Hardware diagnosis and driver/firmware installs | MIT, like the rest of AIRUX | this repository (drivers/) |
| Debian firmware packages (firmware-iwlwifi, -mediatek, -realtek, -atheros, -brcm80211, -amd-graphics, -intel-graphics, -nvidia-graphics, -sof-signed, -misc-nonfree and others) | Firmware loaded by the kernel's drivers | Redistributable, non-free firmware licences of each vendor (see /usr/share/doc/<package>/copyright) | Debian non-free-firmware, unmodified |
| intel-microcode, amd64-microcode | CPU microcode updates | Vendor redistribution licences | Debian non-free-firmware |
| NVIDIA driver (installed on request, not shipped) | GPU driver | NVIDIA licence (non-free); open kernel modules MIT/GPL-2.0 | Debian non-free / contrib |

