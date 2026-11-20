#!/usr/bin/env python3
"""Phagos real-time protection against the real kernel (fanotify), safely: run as root, it re-runs
itself in a private mount namespace (unshare -m) and watches only a scratch tmpfs there, so nothing
else on the machine is affected. Pass requires:

  burst      200 programs started in parallel from the watched folder all run, none waits long
  block      a program on the local blocklist is refused (Operation not permitted)
  eicar      the EICAR test file written there is quarantined
  slow       a checker that hangs makes a program wait about EXEC_BUDGET_S, not forever
  watchdog   events nobody answers are answered by the watchdog within a few seconds
  restart    if the reading thread dies, protection comes back and programs still start
  flood      60,000 files written as fast as possible (at the default limit of 1,024 open files)
             while programs keep starting: none waits long (the VM froze this way once)
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MNT = "/mnt/phagos-rt-test"
EICAR = r"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


def run(path: str) -> tuple[int, float, str]:
    t = time.monotonic()
    # start_new_session: not posix_spawn, which would hold this process's interpreter lock while the
    # child waits for this same process's fanotify answer (see phagos.daemon.spawn)
    try:
        r = subprocess.run([path], capture_output=True, text=True, start_new_session=True)
    except OSError as exc:                       # refused at exec: Python raises instead of a code
        return 126, time.monotonic() - t, str(exc)
    return r.returncode, time.monotonic() - t, r.stderr


def inner() -> int:
    sys.path.insert(0, str(REPO))
    from phagos import daemon as D
    os.makedirs(MNT, exist_ok=True)
    subprocess.run(["mount", "-t", "tmpfs", "-o", "size=64m", "phagos-rt", MNT], check=True)
    D.watched_mounts = lambda: [MNT]
    tmp = Path(tempfile.mkdtemp())
    local = tmp / "rules.d"
    local.mkdir()
    ph = D.Phagos(D.Paths(state=tmp / "state", rules=(REPO / "phagos" / "rules", local), homes=()), train=False)
    rt = D.Realtime(ph)
    rt.start()
    threading.Thread(target=rt.watchdog, daemon=True).start()
    r = {}

    ok = f"{MNT}/ok-tool"
    shutil.copy("/usr/bin/true", ok)
    time.sleep(0.5)
    with cf.ThreadPoolExecutor(32) as ex:
        res = list(ex.map(lambda _: run(ok), range(200)))
    worst = max(t for _, t, _ in res)
    print(f"burst: {sum(c == 0 for c, _, _ in res)}/200 ran; slowest {worst:.2f}s")
    r["burst"] = all(c == 0 for c, _, _ in res) and worst < 3

    bad = f"{MNT}/bad-tool"
    shutil.copy("/usr/bin/true", bad)
    with open(bad, "ab") as f:
        f.write(b"phagos-test-marker")
    (local / "t.sha256").write_text(hashlib.sha256(Path(bad).read_bytes()).hexdigest() + " Test.Blocklisted\n")
    ph.rules = D.RuleSet.load(REPO / "phagos" / "rules", local)
    ph.scanner.rules = ph.rules
    ph.cache.clear()
    code, secs, err = run(bad)
    print(f"block: rc={code} in {secs:.2f}s: {err.strip()[:120]}")
    r["block"] = code != 0 and ("not permitted" in err.lower() or code == 126)

    # written by another process (Phagos ignores its own writes)
    subprocess.run(["sh", "-c", f"printf '%s' '{EICAR}' > {MNT}/eicar.com"], start_new_session=True, check=True)
    gone = False
    for _ in range(30):
        if not Path(f"{MNT}/eicar.com").exists():
            gone = True
            break
        time.sleep(0.2)
    print(f"eicar: quarantined={gone}; history: {[e['threat'] for e in ph.store.events(5, ('threat',))]}")
    r["eicar"] = gone

    real = ph.on_exec
    ph.on_exec = lambda *a: (time.sleep(30), True)[1]           # a checker that hangs
    ph.cache.clear()
    code, secs, _ = run(ok)
    print(f"slow: rc={code} after {secs:.2f}s (budget {D.EXEC_BUDGET_S}s)")
    r["slow"] = code == 0 and secs < D.EXEC_BUDGET_S + 1.5
    ph.on_exec = real

    real_exec = rt._exec
    rt._exec = lambda ev: None                                    # nobody answers
    code, secs, _ = run(ok)
    print(f"watchdog: rc={code} after {secs:.2f}s; stalls healed {rt.stalls}")
    r["watchdog"] = code == 0 and secs < rt.STALL_S + 2 and rt.stalls >= 1
    rt._exec = real_exec

    def boom():
        raise RuntimeError("simulated crash in the reading thread")
    rt.fan.read = boom                                            # the reader dies on its next read
    run(ok)
    time.sleep(2)
    code, secs, _ = run(ok)
    alive = rt.reader is not None and rt.reader.is_alive()
    code2, _, err2 = run(bad)
    print(f"restart: reader alive={alive}; ok rc={code} in {secs:.2f}s; blocklisted rc={code2}")
    r["restart"] = alive and code == 0 and code2 != 0

    import resource
    resource.setrlimit(resource.RLIMIT_NOFILE, (1024, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
    print(f"open-file limit in this test: {resource.getrlimit(resource.RLIMIT_NOFILE)[0]}")
    os.makedirs(f"{MNT}/flood", exist_ok=True)
    writer = subprocess.Popen(["sh", "-c", f"cd {MNT}/flood && i=0; while [ $i -lt 60000 ]; do echo x > f$i; i=$((i+1)); done"],
                              start_new_session=True)
    times = []
    while writer.poll() is None:
        code, secs, _ = run(ok)
        times.append((code, secs))
    writer.wait()
    print(f"flood: {len(times)} programs started during the writes; worst {max(t for _, t in times):.2f}s; "
          f"all ran: {all(c == 0 for c, _ in times)}; backlog now {rt.backlog}, skipped {rt.dropped}")
    r["flood"] = bool(times) and all(c == 0 for c, _ in times) and max(t for _, t in times) < 3

    print("\n=== Phagos real-time check:", "PASS" if all(r.values()) else "FAIL", r)
    return 0 if all(r.values()) else 1


def main() -> int:
    if os.geteuid() != 0:
        print("run as root (it isolates itself with unshare -m)")
        return 1
    if os.environ.get("PHAGOS_RT_INNER") == "1":
        return inner()
    env = dict(os.environ, PHAGOS_RT_INNER="1")
    return subprocess.run(["unshare", "-m", "--propagation", "private", sys.executable, __file__], env=env).returncode


if __name__ == "__main__":
    sys.exit(main())
