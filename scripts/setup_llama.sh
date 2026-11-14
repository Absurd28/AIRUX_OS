#!/usr/bin/env bash
# Build llama.cpp (pinned tag) into $AIRUX_WORK/bin.
# CUDA is used when nvcc is found; otherwise a CPU build. Set AIRUX_CPU_ONLY=1 to force CPU.
set -euo pipefail

LLAMA_TAG="${LLAMA_TAG:-v1.0.0}"
LLAMA_COMMIT="b1b770d90d94cad9e9ea5b993bdbc383a522fb28"   # commit of v1.0.0, checked on clone
AIRUX_WORK="${AIRUX_WORK:-/srv/airux-work}"
SRC="$AIRUX_WORK/src/llama.cpp"
BIN="$AIRUX_WORK/bin"

mkdir -p "$AIRUX_WORK/src" "$BIN"
if [ ! -d "$SRC/.git" ]; then
  git -c http.version=HTTP/1.1 clone --depth 1 --branch "$LLAMA_TAG" \
    https://github.com/ggml-org/llama.cpp.git "$SRC"
fi
got="$(git -C "$SRC" rev-parse HEAD)"
if [ "$LLAMA_TAG" = "v1.0.0" ] && [ "$got" != "$LLAMA_COMMIT" ]; then
  echo "llama.cpp commit mismatch: got $got, want $LLAMA_COMMIT" >&2
  exit 1
fi

NVCC=""
for c in "$(command -v nvcc || true)" /usr/local/cuda/bin/nvcc /usr/local/cuda-13.1/bin/nvcc; do
  if [ -n "$c" ] && [ -x "$c" ]; then NVCC="$c"; break; fi
done

CMAKE_ARGS=(-DCMAKE_BUILD_TYPE=Release -DLLAMA_OPENSSL=OFF -DLLAMA_BUILD_TESTS=OFF)
if [ -n "$NVCC" ] && [ "${AIRUX_CPU_ONLY:-0}" != "1" ]; then
  echo "Building with CUDA: $NVCC"
  # 89 = Ada (RTX 40xx). Override with CUDA_ARCHS for other GPUs.
  CMAKE_ARGS+=(-DGGML_CUDA=ON -DGGML_CUDA_NCCL=OFF
               -DCMAKE_CUDA_COMPILER="$NVCC"
               -DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCHS:-89}")
  BUILD_DIR="$SRC/build-cuda"
else
  echo "Building CPU only"
  BUILD_DIR="$SRC/build-cpu"
  # GGML_NATIVE=OFF so the CPU build runs on any x86-64 machine the ISO lands on
  CMAKE_ARGS+=(-DGGML_NATIVE=OFF)
  BIN="${AIRUX_CPU_BIN:-$AIRUX_WORK/bin-cpu}"
  mkdir -p "$BIN"
fi

cmake -S "$SRC" -B "$BUILD_DIR" "${CMAKE_ARGS[@]}"
cmake --build "$BUILD_DIR" --config Release -j "$(nproc)" \
  --target llama-server llama-cli llama-bench

for t in llama-server llama-cli llama-bench; do
  install -m 0755 "$BUILD_DIR/bin/$t" "$BIN/$t"
done
# shared libs (ggml, llama) sit next to the binaries in build/bin
find "$BUILD_DIR/bin" -maxdepth 1 -name '*.so*' -exec cp -a {} "$BIN/" \;
"$BIN/llama-server" --version
