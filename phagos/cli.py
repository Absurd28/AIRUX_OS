"""phagos: Phagos from a terminal.

  phagos status                     protection state, engines, last scans
  phagos scan [--quick|--full|PATH] run a scan and wait for it
  phagos history [-n 20]            protection history
  phagos quarantine [list|restore ID|delete ID]
  phagos allowed                    files you allowed
  phagos realtime on|off            (administrator)
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time

from .client import Client, PhagosError


def _ts(t) -> str:
    return dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else "never"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="phagos", description="Phagos, AIRUX's antivirus")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sc = sub.add_parser("scan")
    g = sc.add_mutually_exclusive_group()
    g.add_argument("--quick", action="store_true")
    g.add_argument("--full", action="store_true")
    sc.add_argument("path", nargs="?")
    h = sub.add_parser("history")
    h.add_argument("-n", type=int, default=20)
    q = sub.add_parser("quarantine")
    q.add_argument("action", nargs="?", default="list", choices=["list", "restore", "delete"])
    q.add_argument("id", nargs="?")
    sub.add_parser("allowed")
    r = sub.add_parser("realtime")
    r.add_argument("state", choices=["on", "off"])
    a = ap.parse_args(argv)
    c = Client()
    try:
        if a.cmd == "status":
            s = c.call("status")
            e = s["engines"]
            print(f"Phagos {s['version']}: real-time protection {'ON' if s['realtime'] else 'OFF'}"
                  f"{'' if s['protected'] else ' (action needed)'}")
            print(f"  signatures: {e['rules']} rules, {e['blocklist']} blocklisted files")
            print(f"  immune model: {e['self_model']}; {e['self_samples']} self samples, {e['detectors']} detectors "
                  f"({e['memory_detectors']} memory)")
            print(f"  ransomware shield: {s['ransomware_shield']['decoys']} decoys")
            print(f"  threats in 30 days: {s['threats_30_days']}; quarantined: {s['quarantined']}")
            for k, v in s["last_scans"].items():
                if v:
                    print(f"  last {k} scan: {_ts(v['ts'])}, {v['scanned']} files, {v['found']} found")
            for p in s["paused"]:
                print(f"  PAUSED: {p['exe']} (pid {p['pid']}): {'; '.join(p['reasons'])}  [event {p['event_id']}]")
        elif a.cmd == "scan":
            kind = "full" if a.full else ("custom" if a.path else "quick")
            c.call("scan", kind=kind, path=a.path)
            last = -1
            while True:
                st = c.call("scan_status")["scan"]
                if st["scanned"] // 500 != last // 500:
                    print(f"  {st['scanned']} files...", file=sys.stderr)
                last = st["scanned"]
                if st["done"]:
                    break
                time.sleep(1)
            print(f"{kind} scan: {st['scanned']} files, {len(st['found'])} found")
            for f in st["found"]:
                print(f"  {f['level'].upper():10} {f['threat']:32} {f['path']}  ({f['action']})")
            return 2 if st["found"] else 0
        elif a.cmd == "history":
            for e in c.call("history", limit=a.n)["events"]:
                print(f"{_ts(e['ts'])}  {e['kind']:10} {e.get('threat') or '':28} {e.get('path') or '':40} "
                      f"{e.get('action') or ''}")
        elif a.cmd == "quarantine":
            if a.action == "list":
                for i in c.call("quarantine_list")["items"]:
                    print(f"{i['id']}  {_ts(i['ts'])}  {i['threat']:28} {i['path']}")
            elif not a.id:
                ap.error("give the quarantine ID")
            elif a.action == "restore":
                print("restored:", c.call("quarantine_restore", timeout=300, id=a.id)["restored"]["path"])
            else:
                c.call("quarantine_delete", timeout=300, id=a.id)
                print("deleted for good:", a.id)
        elif a.cmd == "allowed":
            for i in c.call("allowed_list")["items"]:
                print(f"{i['sha256'][:16]}…  {i['threat']:28} {i['path']}")
        elif a.cmd == "realtime":
            out = c.call("realtime", timeout=300, on=a.state == "on")
            print("real-time protection", "on" if out["realtime"] else "off")
    except PhagosError as exc:
        print(f"phagos: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
