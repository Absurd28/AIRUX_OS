# AIRUX 0.1

A Debian 13 based OS for developers, engineers, researchers and scientists, with AIRA
(Adaptive Intelligent Research Assistant), a local voice assistant. See `CLAUDE.md` for the
project guide and `PROGRESS.md` for where the build stands.

## Build environment

Development happens in a Debian 13 WSL2 distro (`wsl -d Debian`) on Windows 11. Code lives in
this repo; large files (llama.cpp build, model weights, image build) live in `$AIRUX_WORK`,
default `/srv/airux-work`, inside the distro.

```bash
bash scripts/setup_llama.sh             # build llama-server (CUDA if nvcc is present)
python3 scripts/fetch_models.py         # download and verify models/MANIFEST.toml
python3 tests/phase0_check.py           # phase 0 check: server answers a prompt
```
