"""airux-drivers-helper: the root side of AIRUX Drivers (run with pkexec; polkit action
org.airux.drivers.manage). One action per run; progress lines on stdout, then a last line
"RESULT {json}". Every request is checked here again (the allowlist in actions.py)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import actions

LOG = Path("/var/log/airux-drivers.log")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    import os
    if os.geteuid() != 0:
        print("RESULT " + json.dumps({"ok": False, "error": "the helper must run as root (pkexec)"}))
        return 1
    logf = LOG.open("a")

    def log(line: str) -> None:
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    try:
        if not argv:
            raise actions.ActionError("no action")
        act, args = argv[0], argv[1:]
        if act == "install":
            out = actions.install(args, log)
        elif act == "install_backports":
            out = actions.install(args, log, backports=True)
        elif act == "nvidia":
            out = actions.nvidia(args, log)
        elif act == "unblock":
            out = actions.unblock(args[0] if args else "", log)
        elif act == "collect":
            out = actions.collect(log)
        elif act in ("fwupd_refresh", "fwupd_update"):
            out = actions.fwupd(act == "fwupd_update", log)
        else:
            raise actions.ActionError(f"unknown action {act!r}")
        if act != "collect":
            actions.collect()                      # the picture changed: refresh what users see
        print("RESULT " + json.dumps({"ok": True, **out}), flush=True)
        return 0
    except actions.ActionError as exc:
        log(f"failed: {exc}")
        print("RESULT " + json.dumps({"ok": False, "error": str(exc)}), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
