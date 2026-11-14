"""Runtime configuration from the environment, with defaults for a desktop session."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .persona import load_names


@dataclass
class Config:
    server_url: str = field(default_factory=lambda: os.environ.get("AIRA_SERVER", "http://127.0.0.1:8080"))
    data_dir: Path = field(default_factory=lambda: Path(
        os.environ.get("AIRA_DATA", Path.home() / ".local" / "share" / "aira")))
    # what AIRA calls the user: ~/.config/aira/aira.toml names = [...] or AIRA_USER_NAMES="Aryan,boss"
    user_names: list[str] = field(default_factory=load_names)

    @property
    def user_name(self) -> str:
        return self.user_names[0] if self.user_names else ""
    max_agent_steps: int = 4
    request_timeout: float = 300.0

    @property
    def action_log(self) -> Path:
        return self.data_dir / "actions.jsonl"

    @property
    def assistant_db(self) -> Path:
        return self.data_dir / "assistant.db"

    @property
    def vault_path(self) -> Path:
        return self.data_dir / "vault.db"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
