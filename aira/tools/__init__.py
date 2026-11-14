"""All of AIRA's tools, grouped by sub-agent (at most 8 each)."""
from __future__ import annotations

from ..config import Config
from ..memory.vault import Vault
from . import assistant, desktop, dev, research, system
from .base import Registry


def build_registry(config: Config, vault: Vault, key_source: str = "") -> Registry:
    reg = Registry()
    system.register(reg)
    desktop.register(reg)
    dev.register(reg, vault)
    research.register(reg)
    assistant.register(reg, vault, key_source)
    return reg
