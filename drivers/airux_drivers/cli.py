"""airux-drivers: AIRUX Drivers from a terminal (and for AIRA).

  airux-drivers status [--json]       what works, what does not, and the fix
  airux-drivers report                save a hardware report in your home folder
  airux-drivers fix CATEGORY [--json] run that category's fix (asks for an administrator)
  airux-drivers collect               (root) refresh the firmware failures users can read
"""
from __future__ import annotations

import argparse
import json
import sys

from . import actions, app
from .diagnose import CATEGORIES

ICON = {"working": "OK", "needs_firmware": "FIRMWARE", "off": "OFF", "better_driver": "DRIVER",
        "no_driver": "NO DRIVER", "not_found": "-"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="airux-drivers")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    sub.add_parser("report")
    f = sub.add_parser("fix")
    f.add_argument("category", choices=[c for c, _ in CATEGORIES])
    f.add_argument("--json", action="store_true")
    sub.add_parser("collect")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        st = app.status()
        if a.json:
            print(json.dumps(st, default=str))
            return 0
        sm = st["summary"]
        print("All hardware works." if sm["all_working"] else f"{sm['problems']} problem(s), {sm['fixable']} fixable.")
        for x in st["findings"]:
            fix = f"   fix: airux-drivers fix {x['category']}  ({x['fix']['label']})" if x.get("fix") else ""
            print(f"  {ICON[x['status']]:9} {x['title']:22} {x['device'][:40]:40} {x['detail']}")
            if fix:
                print(fix)
        print(f"  Secure Boot: {st['secure_boot']}")
        return 0
    if a.cmd == "report":
        print(app.save_report())
        return 0
    if a.cmd == "fix":
        out = app.run_fix(a.category, log=(lambda s: None) if a.json else print)
        print(json.dumps(out, default=str) if a.json else ("Done." if out.get("ok") else f"Failed: {out.get('error')}"))
        return 0 if out.get("ok") else 1
    if a.cmd == "collect":
        print(json.dumps(actions.collect()))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
