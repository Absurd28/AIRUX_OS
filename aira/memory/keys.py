"""Vault key: from the desktop keyring when there is one, else a 0600 key file.

With a keyring (gnome-keyring / KWallet via the Secret Service, unlocked by PAM at login) the
vault is unlocked at login. Without one (e.g. WSL), the key lives in ~/.config/aira/vault.key,
readable only by the user: that protects against other users and a copied vault file, not
against someone who can read the user's home folder.
"""
from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from pathlib import Path

ATTRS = ["service", "aira", "item", "vault-key"]


def keyring_unlocked() -> bool:
    """True only if the default Secret Service collection exists and is already unlocked.
    Using a locked or missing keyring would pop up a password dialog (e.g. with automatic login,
    or in the live session), so AIRA then uses its key file instead."""
    if not shutil.which("gdbus") or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return False
    try:
        r = subprocess.run(["gdbus", "call", "--session", "--timeout", "3", "--dest", "org.freedesktop.secrets",
                            "--object-path", "/org/freedesktop/secrets/aliases/default", "--method",
                            "org.freedesktop.DBus.Properties.Get", "org.freedesktop.Secret.Collection", "Locked"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and "false" in r.stdout


def _keyring_get() -> str | None:
    if not shutil.which("secret-tool") or not keyring_unlocked():
        return None
    try:
        r = subprocess.run(["secret-tool", "lookup", *ATTRS], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _keyring_set(key: str) -> bool:
    if not shutil.which("secret-tool") or not keyring_unlocked():
        return False
    try:
        r = subprocess.run(["secret-tool", "store", "--label=AIRA memory vault", *ATTRS],
                           input=key, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def key_file() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "aira" / "vault.key"


def get_key(create: bool = True) -> tuple[str, str]:
    """Return (key, source) where source is 'keyring' or 'file'."""
    k = _keyring_get()
    if k:
        return k, "keyring"
    f = key_file()
    if f.exists():
        return f.read_text().strip(), "file"
    if not create:
        raise FileNotFoundError("no vault key in the keyring or key file")
    k = secrets.token_hex(32)
    if _keyring_set(k):
        return k, "keyring"
    f.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(k + "\n")
    return k, "file"
