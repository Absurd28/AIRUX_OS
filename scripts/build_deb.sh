#!/usr/bin/env bash
# Build aira_<version>_amd64.deb and aira-models-base_<version>_all.deb into $AIRUX_WORK/build/deb.
# Run as root on Debian 13 (the venv is built at its final path, /opt/aira/venv).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${AIRA_VERSION:-0.1.0}"
WORK="${AIRUX_WORK:-/srv/airux-work}"
OUT="$WORK/build/deb"
STAGE="$OUT/aira"
MSTAGE="$OUT/aira-models-base"
P="$REPO/packaging"

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
rm -rf "$STAGE" "$MSTAGE"
mkdir -p "$STAGE/DEBIAN" "$MSTAGE/DEBIAN" "$STAGE/opt/aira/app/models"

echo "== app"
cp -r "$REPO/aira" "$STAGE/opt/aira/app/"
find "$STAGE/opt/aira/app" -name __pycache__ -prune -exec rm -rf {} +
cp "$REPO/models/MANIFEST.toml" "$STAGE/opt/aira/app/models/"

echo "== pinned virtual environment (at its final path)"
rm -rf /opt/aira/venv
mkdir -p /opt/aira
python3 -m venv /opt/aira/venv
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 /opt/aira/venv/bin/pip install --no-cache-dir --disable-pip-version-check -q \
    -r "$REPO/requirements.lock"
/opt/aira/venv/bin/python - "$REPO/requirements.lock" <<'PY'
import re, subprocess, sys
norm = lambda s: re.sub(r"[-_.]+", "-", s.strip().lower())
want = {norm(l.split("==")[0]): l.split("==")[1].strip() for l in open(sys.argv[1]) if re.match(r"^[A-Za-z0-9]", l)}
got = {}
for l in subprocess.run(["/opt/aira/venv/bin/pip", "freeze", "--all"], capture_output=True, text=True).stdout.splitlines():
    if "==" in l:
        n, v = l.split("==")
        if norm(n) not in ("pip", "setuptools", "wheel"):
            got[norm(n)] = v
extra = sorted(set(got) - set(want)); missing = sorted(set(want) - set(got))
wrong = sorted(n for n in set(want) & set(got) if want[n] != got[n])
if extra or missing or wrong:
    sys.exit(f"venv does not match requirements.lock: extra={extra} missing={missing} wrong={wrong}")
print(f"venv matches requirements.lock exactly ({len(got)} packages)")
PY
/opt/aira/venv/bin/python -c "import sqlcipher3, sqlite_vec, playwright, nbclient; print('imports ok')"
cp -a /opt/aira/venv "$STAGE/opt/aira/venv"

echo "== llama.cpp (portable CPU build) and whisper.cpp"
mkdir -p "$STAGE/opt/aira/bin-cpu" "$STAGE/opt/aira/bin-whisper"
cp -a "$WORK/bin-cpu/llama-server" "$WORK"/bin-cpu/*.so* "$STAGE/opt/aira/bin-cpu/"
cp -a "$WORK/bin-whisper/whisper-server" "$WORK"/bin-whisper/*.so* "$STAGE/opt/aira/bin-whisper/"
# llama-server is a thin launcher over libllama-server-impl.so: keep that one, drop the bench/cli helpers
rm -f "$STAGE"/opt/aira/bin-cpu/libllama-bench-impl.so "$STAGE"/opt/aira/bin-cpu/libllama-cli-impl.so
LD_LIBRARY_PATH="$STAGE/opt/aira/bin-cpu" "$STAGE/opt/aira/bin-cpu/llama-server" --version >/dev/null
LD_LIBRARY_PATH="$STAGE/opt/aira/bin-whisper" "$STAGE/opt/aira/bin-whisper/whisper-server" --help >/dev/null 2>&1

echo "== commands, services, polkit, autostart"
install -Dm755 "$P/lib/run" "$STAGE/usr/lib/aira/run"
install -Dm755 "$P/lib/aira-stt" "$STAGE/usr/lib/aira/aira-stt"
for f in "$P"/bin/*; do install -Dm755 "$f" "$STAGE/usr/bin/$(basename "$f")"; done
for f in "$P"/systemd/system/*.service; do install -Dm644 "$f" "$STAGE/usr/lib/systemd/system/$(basename "$f")"; done
for f in "$P"/systemd/user/*.service; do install -Dm644 "$f" "$STAGE/usr/lib/systemd/user/$(basename "$f")"; done
install -Dm644 "$P/polkit/50-aira.rules" "$STAGE/usr/share/polkit-1/rules.d/50-aira.rules"
install -Dm644 "$P/xdg/autostart/aira-shell.desktop" "$STAGE/etc/xdg/autostart/aira-shell.desktop"
install -Dm644 "$P/xdg/autostart/aira-shell.desktop" "$STAGE/usr/share/applications/aira.desktop"
sed -i '/^OnlyShowIn=/d;/^X-GNOME-Autostart/d' "$STAGE/usr/share/applications/aira.desktop"
install -Dm644 "$P/branding/aira-icon.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/aira.svg"
mkdir -p "$STAGE/usr/share/icons/hicolor/256x256/apps"
rsvg-convert -w 256 -h 256 "$P/branding/aira-icon.svg" -o "$STAGE/usr/share/icons/hicolor/256x256/apps/aira.png"
install -Dm644 "$REPO/README.md" "$STAGE/usr/share/doc/aira/README.md"
install -Dm644 "$REPO/LICENSES.md" "$STAGE/usr/share/doc/aira/LICENSES.md"
for s in postinst prerm postrm; do install -m755 "$P/debian/$s" "$STAGE/DEBIAN/$s"; done

SIZE=$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)
cat > "$STAGE/DEBIAN/control" <<EOF
Package: aira
Version: $VERSION
Architecture: amd64
Maintainer: AIRUX <airux@localhost>
Installed-Size: $SIZE
Depends: python3 (>= 3.13), python3-venv, libgomp1, adduser, systemd, polkitd | policykit-1, packagekit,
 chromium, pulseaudio-utils, libnotify-bin, poppler-utils, libsecret-tools, git, curl
Recommends: aira-models-base (= $VERSION), xfce4-notifyd | notification-daemon, gnome-keyring, podman,
 python3-notebook | jupyter-notebook
Section: utils
Priority: optional
Description: AIRA, the Adaptive Intelligent Research Assistant for AIRUX
 A local voice assistant: one Qwen3.5 model served by llama.cpp, five sub-agents, a permission
 broker that asks before anything risky, a grounding guard against made-up answers, an encrypted
 memory vault, reminders, voice (whisper.cpp, Kokoro) and a full-screen shell.
EOF
dpkg-deb --root-owner-group -Zxz -z6 --build "$STAGE" "$OUT/aira_${VERSION}_amd64.deb" >/dev/null

echo "== models (2B tier, embeddings, speech in and out)"
mkdir -p "$MSTAGE/opt/aira/models"
for f in Qwen3.5-2B-Q4_K_M.gguf nomic-embed-text-v1.5.Q8_0.gguf kokoro-v1.0.onnx voices-v1.0.bin \
         ggml-base.en.bin en_US-ljspeech-medium.onnx en_US-ljspeech-medium.onnx.json; do
    cp "$WORK/models/$f" "$MSTAGE/opt/aira/models/"
done
MSIZE=$(du -sk --exclude=DEBIAN "$MSTAGE" | cut -f1)
cat > "$MSTAGE/DEBIAN/control" <<EOF
Package: aira-models-base
Version: $VERSION
Architecture: all
Maintainer: AIRUX <airux@localhost>
Installed-Size: $MSIZE
Section: utils
Priority: optional
Description: Base models for AIRA (Qwen3.5 2B, nomic-embed v1.5, Kokoro-82M, whisper base.en, Piper LJSpeech)
 Enough to run AIRA on any machine. Bigger language models for more capable hardware are offered
 at first boot and downloaded with aira-models only when the user asks. Licences in
 /usr/share/doc/aira/LICENSES.md.
EOF
dpkg-deb --root-owner-group -Znone --build "$MSTAGE" "$OUT/aira-models-base_${VERSION}_all.deb" >/dev/null

ls -la "$OUT"/*.deb
dpkg-deb -I "$OUT/aira_${VERSION}_amd64.deb" | sed -n '1,12p'
