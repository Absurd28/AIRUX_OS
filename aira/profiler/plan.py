"""Choose model tier and slot count from measured hardware.

Tier rule (CLAUDE.md, with the 16-24 GB gap closed as agreed in phase 0):
  total memory = RAM + VRAM
  GPU with >= 24 GiB VRAM -> 27B
  total <  6 GiB          -> 2B
  6 <= total < 8 GiB      -> 4B
  total >= 8 GiB          -> 9B
The chosen tier then steps down while its model is not downloaded or does not fit in
free memory with the reserve, and each step is recorded as a reason.

Slots (CLAUDE.md):
  slots = (free memory - model size - 2 GiB reserve) / per-slot context memory,
  capped by CPU cores, minimum 1.
Per-slot memory comes from the GGUF metadata: KV cache for the full-attention layers
for `ctx_per_slot` tokens, plus the fixed recurrent state of the linear-attention layers.
"""
from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .gguf import read_metadata
from .probe import GiB, Hardware

TIER_ORDER = ["2B", "4B", "9B", "27B"]
RESERVE = 2 * GiB
CACHE_RAM_MAX = 1 * GiB
DEFAULT_CTX_PER_SLOT = 8192
F16 = 2
F32 = 4
REPO = Path(__file__).resolve().parents[2]


@dataclass
class ModelInfo:
    tier: str
    name: str
    path: Path
    size: int  # bytes on disk (weights loaded ~ this size)


@dataclass
class Plan:
    tier_by_rule: str
    tier: str
    model: str
    model_path: str
    model_size: int
    use_gpu: bool
    slots: int
    slots_uncapped: int
    ctx_per_slot: int
    per_slot_bytes: int
    cache_ram_mib: int = 0   # llama-server --cache-ram (host prompt cache; its default 8192 MiB OOMs small machines)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def tier_for(hw: Hardware) -> str:
    if hw.vram_total >= 24 * GiB:
        return "27B"
    total = hw.total_memory
    if total < 6 * GiB:
        return "2B"
    if total < 8 * GiB:
        return "4B"
    return "9B"


def load_catalogue(models_dir: Path, manifest: Path = REPO / "models" / "MANIFEST.toml") -> dict[str, ModelInfo]:
    """Tier -> ModelInfo for LLM entries in the manifest whose file exists."""
    out: dict[str, ModelInfo] = {}
    for e in tomllib.loads(manifest.read_text())["model"]:
        if e.get("role") != "llm":
            continue
        p = models_dir / e["file"]
        if p.is_file():
            out[e["tier"]] = ModelInfo(e["tier"], e["name"], p, p.stat().st_size)
    return out


def per_slot_bytes(meta: dict, ctx_per_slot: int, kv_bytes: int = F16) -> int:
    """Memory one slot needs: KV cache (full-attention layers) + recurrent state."""
    arch = meta["general.architecture"]
    g = lambda k, d=None: meta.get(f"{arch}.{k}", d)  # noqa: E731
    n_layers = int(g("block_count"))
    interval = int(g("full_attention_interval", 1) or 1)
    n_full = n_layers // interval if interval > 1 else n_layers
    n_linear = n_layers - n_full
    n_kv = g("attention.head_count_kv")
    n_kv = max(n_kv) if isinstance(n_kv, list) else int(n_kv)
    head_dim = int(g("embedding_length")) // int(g("attention.head_count"))
    k_len = int(g("attention.key_length", head_dim))
    v_len = int(g("attention.value_length", head_dim))
    kv = n_full * n_kv * (k_len + v_len) * kv_bytes * ctx_per_slot

    rs = 0
    if n_linear and g("ssm.state_size"):
        d_state = int(g("ssm.state_size"))          # head dim of the delta-net state
        n_v_heads = int(g("ssm.time_step_rank"))     # value heads
        n_k_heads = int(g("ssm.group_count"))        # key heads
        d_inner = int(g("ssm.inner_size"))           # value dim total
        conv_dim = n_k_heads * d_state * 2 + d_inner
        ssm_state = n_v_heads * d_state * d_state * F32
        conv_state = (int(g("ssm.conv_kernel")) - 1) * conv_dim * F32
        rs = n_linear * (ssm_state + conv_state)
    return kv + rs


def plan(hw: Hardware, models_dir: Path, ctx_per_slot: int = DEFAULT_CTX_PER_SLOT,
         max_tier: str | None = None, reasons: list[str] | None = None,
         max_slots: int | None = None) -> Plan:
    """max_tier caps the tier (used when a measured speed test forces a step down).
    max_slots caps the slot count (tuning knob; see PROGRESS.md phase 1 measurements)."""
    catalogue = load_catalogue(models_dir)
    if not catalogue:
        raise FileNotFoundError(f"no LLM model files found in {models_dir}")
    by_rule = tier_for(hw)
    reasons = list(reasons or [])
    reasons.append(f"total memory {hw.total_memory / GiB:.1f} GiB "
                   f"(RAM {hw.ram_total / GiB:.1f} + VRAM {hw.vram_total / GiB:.1f}) -> {by_rule} by rule")

    idx = TIER_ORDER.index(by_rule)
    if max_tier is not None and TIER_ORDER.index(max_tier) < idx:
        idx = TIER_ORDER.index(max_tier)
        reasons.append(f"capped at {max_tier}")
    chosen: ModelInfo | None = None
    while idx >= 0:
        tier = TIER_ORDER[idx]
        m = catalogue.get(tier)
        if m is None:
            reasons.append(f"{tier} model not downloaded; stepping down")
        elif m.size + RESERVE > hw.free_memory:
            reasons.append(f"{tier} needs {(m.size + RESERVE) / GiB:.1f} GiB with reserve, "
                           f"only {hw.free_memory / GiB:.1f} GiB free; stepping down")
        else:
            chosen = m
            break
        idx -= 1
    if chosen is None:
        chosen = catalogue[min(catalogue, key=TIER_ORDER.index)]
        reasons.append(f"nothing fits; using smallest available model {chosen.tier} anyway")

    psb = per_slot_bytes(read_metadata(chosen.path), ctx_per_slot)
    spare = hw.free_memory - chosen.size - RESERVE
    uncapped = max(0, spare // psb)
    slots = max(1, min(uncapped, hw.cpu_cores))
    reasons.append(f"slots: ({hw.free_memory / GiB:.2f} free - {chosen.size / GiB:.2f} model - 2 reserve) GiB "
                   f"/ {psb / 2**20:.0f} MiB per slot = {uncapped}, capped by {hw.cpu_cores} cores -> {slots}")
    if max_slots is not None and slots > max_slots:
        slots = max(1, max_slots)
        reasons.append(f"slots capped at {slots} by --max-slots")
    # RAM prompt cache: only what is left in RAM after the whole model, reserve and slots
    # (counted as if all in RAM, the worst case), capped at CACHE_RAM_MAX.
    ram_spare = hw.ram_available - chosen.size - RESERVE - slots * psb
    cache_ram_mib = int(max(0, min(ram_spare, CACHE_RAM_MAX)) // 2**20)
    reasons.append(f"RAM prompt cache {cache_ram_mib} MiB (spare RAM {ram_spare / GiB:.2f} GiB, cap "
                   f"{CACHE_RAM_MAX // 2**20} MiB)")
    return Plan(
        tier_by_rule=by_rule, tier=chosen.tier, model=chosen.name, model_path=str(chosen.path),
        model_size=chosen.size, use_gpu=hw.vram_total > 0, slots=slots, slots_uncapped=int(uncapped),
        ctx_per_slot=ctx_per_slot, per_slot_bytes=psb, cache_ram_mib=cache_ram_mib, reasons=reasons,
    )
