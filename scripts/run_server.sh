#!/usr/bin/env bash
# Start llama-server for AIRA. Phase 1's profiler will choose MODEL, NGL, SLOTS and CTX;
# until then these are plain defaults.
#   MODEL  path to a GGUF (default: the 2B model)
#   NGL    layers to offload to GPU (default 99 = all that fit; 0 = CPU only)
#   SLOTS  parallel slots (default 1)
#   CTX    total context across all slots (default 8192)
#   PORT   (default 8080), HOST (default 127.0.0.1: never exposed off the machine)
#   LLAMA_BIN  directory holding llama-server (default $AIRUX_WORK/bin)
set -euo pipefail

AIRUX_WORK="${AIRUX_WORK:-/srv/airux-work}"
MODEL="${MODEL:-$AIRUX_WORK/models/Qwen3.5-2B-Q4_K_M.gguf}"
BIN="${LLAMA_BIN:-$AIRUX_WORK/bin}"   # bin = CUDA build, bin-cpu = portable CPU build

if [ ! -f "$MODEL" ]; then
  echo "Model not found: $MODEL" >&2
  exit 1
fi

export LD_LIBRARY_PATH="$BIN:/usr/lib/wsl/lib:${LD_LIBRARY_PATH:-}"
exec "$BIN/llama-server" \
  --model "$MODEL" \
  --host "${HOST:-127.0.0.1}" --port "${PORT:-8080}" \
  --jinja \
  --reasoning off \
  --n-gpu-layers "${NGL:-99}" \
  --parallel "${SLOTS:-1}" \
  --ctx-size "${CTX:-8192}" \
  "$@"
