#!/usr/bin/env bash
# Build whisper.cpp (pinned tag) into $AIRUX_WORK/bin-whisper: whisper-server and whisper-cli.
# Portable CPU build (GGML_NATIVE=OFF): speech recognition stays off the GPU, which the language
# model needs, and the same binary runs on any x86-64 machine the ISO is installed on.
set -euo pipefail

WHISPER_TAG="${WHISPER_TAG:-v1.9.5}"
WHISPER_COMMIT="d1be6fde11ac6e0407606b4e42fe72d34add8037"   # commit of v1.9.5, checked on clone
AIRUX_WORK="${AIRUX_WORK:-/srv/airux-work}"
SRC="$AIRUX_WORK/src/whisper.cpp"
BIN="$AIRUX_WORK/bin-whisper"

mkdir -p "$AIRUX_WORK/src" "$BIN"
if [ ! -d "$SRC/.git" ]; then
  git -c http.version=HTTP/1.1 clone --depth 1 --branch "$WHISPER_TAG" \
    https://github.com/ggml-org/whisper.cpp.git "$SRC"
fi
got="$(git -C "$SRC" rev-parse HEAD)"
if [ "$WHISPER_TAG" = "v1.9.5" ] && [ "$got" != "$WHISPER_COMMIT" ]; then
  echo "whisper.cpp commit mismatch: got $got, want $WHISPER_COMMIT" >&2
  exit 1
fi

cmake -S "$SRC" -B "$SRC/build-cpu" -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=OFF \
  -DWHISPER_BUILD_SERVER=ON -DWHISPER_BUILD_EXAMPLES=ON -DWHISPER_BUILD_TESTS=OFF -DWHISPER_SDL2=OFF
cmake --build "$SRC/build-cpu" --config Release -j "$(nproc)" --target whisper-server whisper-cli

for t in whisper-server whisper-cli; do
  install -m 0755 "$SRC/build-cpu/bin/$t" "$BIN/$t"
done
find "$SRC/build-cpu" -name '*.so*' -exec cp -a {} "$BIN/" \;
LD_LIBRARY_PATH="$BIN" "$BIN/whisper-cli" --help 2>&1 | head -3
