"""Where AIROX Connect keeps things, and its settings."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

PORT = 8766              # HTTPS + WebSocket, for paired phones (LAN only)
DISCOVERY_PORT = 8767    # UDP: a paired phone finds the PC again when its address changed
APP_PORT = 8768          # plain HTTP, only while "Get the app" is shown: the APK and nothing else
DEFAULTS = {"enabled": False, "mirror_all": False, "pc_silent": True, "name": ""}


def state_dir() -> Path:
    d = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "airox-connect"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or str(state_dir() / "run")
    d = Path(base) / "airox-connect"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def downloads_dir() -> Path:
    try:
        import subprocess
        r = subprocess.run(["xdg-user-dir", "DOWNLOAD"], capture_output=True, text=True, timeout=5)
        base = Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else Path.home() / "Downloads"
    except (OSError, ValueError):
        base = Path.home() / "Downloads"
    if base == Path.home():
        base = Path.home() / "Downloads"
    return base / "AIROX Connect"


def private_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = path or state_dir() / "settings.json"
        self.lock = threading.Lock()
        try:
            self.data = {**DEFAULTS, **json.loads(self.path.read_text())}
        except (OSError, ValueError):
            self.data = dict(DEFAULTS)

    def get(self, key: str):
        with self.lock:
            return self.data.get(key, DEFAULTS.get(key))

    def set(self, **kw) -> dict:
        with self.lock:
            for k, v in kw.items():
                if k not in DEFAULTS:
                    raise KeyError(k)
                self.data[k] = v
            private_write(self.path, json.dumps(self.data, indent=1))
            return dict(self.data)

    def all(self) -> dict:
        with self.lock:
            return dict(self.data)
