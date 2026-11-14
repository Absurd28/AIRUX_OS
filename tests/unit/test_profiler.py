"""Unit tests for aira.profiler (no server, no real models needed except where marked)."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from aira.profiler import plan as planmod
from aira.profiler.gguf import GGUFError, read_metadata
from aira.profiler.probe import GiB, MiB, Hardware, cgroup_memory, count_cores, parse_meminfo

REAL_MODELS = Path("/srv/airux-work/models")


# ---------- GGUF reader ----------

def _gguf_bytes(kv: list[tuple[str, int, bytes]]) -> bytes:
    out = b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0) + struct.pack("<Q", len(kv))
    for key, vtype, payload in kv:
        k = key.encode()
        out += struct.pack("<Q", len(k)) + k + struct.pack("<I", vtype) + payload
    return out


def _s(v: str) -> bytes:
    b = v.encode()
    return struct.pack("<Q", len(b)) + b


def test_gguf_reads_scalars_strings_arrays(tmp_path):
    long_arr = struct.pack("<IQ", 4, 1000) + struct.pack("<1000I", *range(1000))
    f = tmp_path / "t.gguf"
    f.write_bytes(_gguf_bytes([
        ("general.architecture", 8, _s("qwen35")),
        ("qwen35.block_count", 4, struct.pack("<I", 24)),
        ("qwen35.rope.freq_base", 6, struct.pack("<f", 1e7)),
        ("short", 9, struct.pack("<IQ", 5, 3) + struct.pack("<3i", 1, -2, 3)),
        ("long", 9, long_arr),
        ("after", 7, struct.pack("<?", True)),
    ]))
    md = read_metadata(f)
    assert md["general.architecture"] == "qwen35"
    assert md["qwen35.block_count"] == 24
    assert md["short"] == [1, -2, 3]
    assert md["long"] == {"array_len": 1000, "item_type": 4}
    assert md["after"] is True  # parsing continued correctly after skipping the long array


def test_gguf_rejects_non_gguf(tmp_path):
    f = tmp_path / "x.bin"
    f.write_bytes(b"NOPE" + b"\0" * 32)
    with pytest.raises(GGUFError):
        read_metadata(f)


def test_gguf_truncated(tmp_path):
    f = tmp_path / "t.gguf"
    f.write_bytes(_gguf_bytes([("general.architecture", 8, _s("qwen35"))])[:-3])
    with pytest.raises(GGUFError):
        read_metadata(f)


# ---------- probe parsing ----------

def test_parse_meminfo():
    total, avail = parse_meminfo("MemTotal:  7980124 kB\nMemFree: 1 kB\nMemAvailable:  7379436 kB\n")
    assert total == 7980124 * 1024 and avail == 7379436 * 1024


def test_count_cores_hyperthreaded():
    blocks = []
    for i in range(12):
        blocks.append(f"processor\t: {i}\nphysical id\t: 0\ncore id\t\t: {i // 2}\n")
    cores, threads = count_cores("\n".join(blocks))
    assert (cores, threads) == (6, 12)


def test_cgroup_limit_tightest_ancestor(tmp_path):
    root = tmp_path
    (root / "a" / "b").mkdir(parents=True)
    (root / "memory.max").write_text("max\n")
    (root / "a" / "memory.max").write_text(str(4 * GiB))
    (root / "a" / "memory.current").write_text(str(1 * GiB))
    (root / "a" / "b" / "memory.max").write_text("max\n")
    limit, current = cgroup_memory("0::/a/b\n", root)
    assert limit == 4 * GiB and current == 1 * GiB


def test_cgroup_no_limit(tmp_path):
    (tmp_path / "memory.max").write_text("max\n")
    assert cgroup_memory("0::/\n", tmp_path) == (None, None)


# ---------- tier rule ----------

def hw(ram_gb: float, vram_gb: float = 0, free_ram_gb: float | None = None, cores: int = 6) -> Hardware:
    ram = int(ram_gb * GiB)
    vram = int(vram_gb * GiB)
    free = int((free_ram_gb if free_ram_gb is not None else ram_gb * 0.9) * GiB)
    return Hardware(ram_total=ram, ram_available=free, vram_total=vram, vram_free=vram, cpu_cores=cores)


@pytest.mark.parametrize("ram,vram,tier", [
    (4, 0, "2B"), (5.99, 0, "2B"),
    (6, 0, "4B"), (7.9, 0, "4B"),
    (8, 0, "9B"), (15.7, 0, "9B"),
    (15.7, 6, "9B"),           # this laptop on bare metal: 21.7 GiB, in the old 16-24 gap
    (7.6, 6, "9B"),            # this laptop under WSL (7.6 GiB RAM) + RTX 4050
    (32, 16, "9B"),            # 48 GiB total but no 24 GiB GPU
    (32, 24, "27B"), (64, 48, "27B"),
])
def test_tier_rule(ram, vram, tier):
    assert planmod.tier_for(hw(ram, vram)) == tier


# ---------- plan: step-down, slots ----------

QWEN35_2B_META = {
    "general.architecture": "qwen35", "qwen35.block_count": 24, "qwen35.embedding_length": 2048,
    "qwen35.attention.head_count": 8, "qwen35.attention.head_count_kv": 2,
    "qwen35.attention.key_length": 256, "qwen35.attention.value_length": 256,
    "qwen35.full_attention_interval": 4, "qwen35.ssm.state_size": 128,
    "qwen35.ssm.time_step_rank": 16, "qwen35.ssm.group_count": 16,
    "qwen35.ssm.inner_size": 2048, "qwen35.ssm.conv_kernel": 4,
}


def test_per_slot_bytes_qwen35_2b():
    psb = planmod.per_slot_bytes(QWEN35_2B_META, 8192)
    kv = 6 * 2 * 512 * 2 * 8192                       # 6 full-attn layers, f16 K+V
    rs = 18 * (16 * 128 * 128 * 4 + 3 * (16 * 128 * 2 + 2048) * 4)
    assert psb == kv + rs
    assert 110 * MiB < psb < 120 * MiB


@pytest.fixture
def fake_models(tmp_path, monkeypatch):
    """Model dir with sparse files of the real sizes; metadata comes from the 2B dict."""
    sizes = {"Qwen3.5-2B-Q4_K_M.gguf": 1280835840, "Qwen3.5-4B-Q4_K_M.gguf": 2740937888,
             "Qwen3.5-9B-Q4_K_M.gguf": 5680000000}
    for name, size in sizes.items():
        with open(tmp_path / name, "wb") as f:
            f.truncate(size)
    monkeypatch.setattr(planmod, "read_metadata", lambda p: QWEN35_2B_META)
    return tmp_path


def test_plan_this_laptop_wsl(fake_models):
    p = planmod.plan(hw(7.6, 6, free_ram_gb=7.0), fake_models)
    assert p.tier == "9B" and p.use_gpu
    assert p.slots == 6  # memory allows many; capped by 6 cores


def test_plan_steps_down_when_model_missing(fake_models):
    (fake_models / "Qwen3.5-9B-Q4_K_M.gguf").unlink()
    p = planmod.plan(hw(15.7, 6), fake_models)
    assert p.tier_by_rule == "9B" and p.tier == "4B"
    assert any("9B model not downloaded" in r for r in p.reasons)


def test_plan_27b_rule_falls_back_when_not_downloaded(fake_models):
    p = planmod.plan(hw(64, 24), fake_models)
    assert p.tier_by_rule == "27B" and p.tier == "9B"


def test_plan_steps_down_when_free_memory_short(fake_models):
    # 8 GiB total -> 9B by rule, but only 6.5 GiB free: 9B (5.3 GiB + 2 reserve) does not fit
    p = planmod.plan(hw(8, 0, free_ram_gb=6.5), fake_models)
    assert p.tier_by_rule == "9B" and p.tier == "4B"


def test_plan_slots_formula_and_minimum(fake_models):
    # 4 GiB, 3.4 free: 3.4 - 1.19 - 2 = 0.2 GiB spare -> 1 slot of ~115 MiB
    p = planmod.plan(hw(4, 0, free_ram_gb=3.4), fake_models)
    assert p.tier == "2B"
    spare = int(3.4 * GiB) - 1280835840 - 2 * GiB
    assert p.slots_uncapped == spare // p.per_slot_bytes
    assert p.slots == max(1, min(p.slots_uncapped, 6))
    # nothing spare at all -> still 1
    p2 = planmod.plan(hw(4, 0, free_ram_gb=3.2), fake_models)
    assert p2.slots_uncapped == 0 and p2.slots == 1


def test_plan_cache_ram_bounded(fake_models):
    # this laptop under WSL: 7 GiB free RAM cannot hold 9B (5.3) + reserve (2) -> no RAM cache
    p = planmod.plan(hw(7.6, 6, free_ram_gb=7.0), fake_models)
    assert p.cache_ram_mib == 0
    # plenty of RAM -> capped at 1 GiB
    p = planmod.plan(hw(64, 0, free_ram_gb=60), fake_models)
    assert p.cache_ram_mib == 1024
    # in between -> exactly the spare
    p = planmod.plan(hw(16, 0, free_ram_gb=8.3), fake_models)
    spare = int(8.3 * GiB) - p.model_size - 2 * GiB - p.slots * p.per_slot_bytes
    assert p.cache_ram_mib == max(0, min(spare, GiB)) // 2**20


def test_plan_max_slots(fake_models):
    p = planmod.plan(hw(7.6, 6, free_ram_gb=7.0), fake_models, max_slots=2)
    assert p.slots == 2 and p.slots_uncapped > 2


def test_plan_max_tier(fake_models):
    p = planmod.plan(hw(15.7, 6), fake_models, max_tier="4B")
    assert p.tier == "4B" and any("capped at 4B" in r for r in p.reasons)


# ---------- real model files (skipped if not downloaded) ----------

@pytest.mark.skipif(not (REAL_MODELS / "Qwen3.5-2B-Q4_K_M.gguf").exists(), reason="2B not downloaded")
def test_real_2b_metadata_matches_fixture():
    md = read_metadata(REAL_MODELS / "Qwen3.5-2B-Q4_K_M.gguf")
    for k, v in QWEN35_2B_META.items():
        assert md[k] == v, k
