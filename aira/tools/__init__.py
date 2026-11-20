"""All of AIRA's tools, grouped by sub-agent (8 each to start; Desktop has 9, System 10)."""
from __future__ import annotations

from ..config import Config
from ..memory.vault import Vault
from . import assistant, desktop, dev, hardware, research, security, system
from .base import Registry


def build_registry(config: Config, vault: Vault, key_source: str = "", llm=None, pool=None,
                   browser=None) -> Registry:
    reg = Registry()
    system.register(reg)
    security.register(reg)      # Phagos, the antivirus (System agent)
    hardware.register(reg)      # AIRUX Drivers (System agent)
    desktop.register(reg, browser)
    dev.register(reg, vault)
    research.register(reg, pool, llm)
    assistant.register(reg, vault, key_source)
    return reg
