"""All of AIRA's tools, grouped by sub-agent (at most 8 each)."""
from __future__ import annotations

from ..config import Config
from . import assistant, desktop, dev, research, system
from .base import Registry


def build_registry(config: Config, store: assistant.Store | None = None) -> Registry:
    reg = Registry()
    system.register(reg)
    desktop.register(reg)
    dev.register(reg)
    research.register(reg)
    assistant.register(reg, store or assistant.Store(config.assistant_db))
    return reg
