#!/usr/bin/env python3
"""Resource use of AIRUX's services and apps in QEMU (live ISO). Measures only; changes nothing.
Run as root in WSL:  measure_resources.py [--boot]

Boots the ISO (or uses the running VM), opens NEOX (for its native host), lets the system idle 5
minutes after AIRA is ready, then samples /proc every 5 s for a minute in each phase:
  idle      nothing asked
  aira      while AIRA answers one typed question
  scan      during a Phagos quick scan
  nomodel   with aira-llm stopped (total RAM without the model), then started again
Per component: RSS and PSS (MiB) and CPU % of one core, averaged over the samples. Output:
$AIRUX_WORK/build/vm/resources.json and a Markdown table on stdout.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
import desktop_check as dc  # noqa: E402

vm, D, sh, p8 = dc.vm, dc.D, dc.sh, dc.p8

# components: label -> regular expression on the process command line (Python or native noted)
COMPONENTS = [
    ("AIRA (side panel, orchestrator, agents)", r"aira\.shell", "Python"),
    ("AIRA voice worker (Kokoro)", r"aira\.tts_worker", "Python + ONNX Runtime (C++)"),
    ("AIRA reminder daemon", r"aira\.reminderd", "Python"),
    ("llama-server, chat model", r"llama-server .*--port 8080", "C++"),
    ("llama-server, embeddings", r"llama-server .*--port 8081", "C++"),
    ("whisper-server", r"whisper-server", "C++"),
    ("aira-llm launcher (profiler)", r"aira\.profiler --exec", "Python"),
    ("phagosd", r"phagos\.daemon|/usr/sbin/phagosd", "Python"),
    ("phagos-agent", r"phagos\.agent|phagos-agent", "Python"),
    ("neox-vpnd", r"neox-vpnd|neox_vpn|vpn\.daemon", "Python"),
    ("NEOX native host", r"aira\.neox_host|neox-host", "Python"),
    ("NEOX (Chromium, all processes)", r"chromium.*--class=neox|chromium.*user-data-dir=.*neox", "C++"),
    ("AIROX Connect (airox-connectd)", r"airox_connect\.server", "Python"),
    ("AIROX Connect desktop helper", r"airox_connect\.portal_helper", "Python"),
    ("AIRUX Drivers (timer/app, when running)", r"airux_drivers|airux-drivers", "Python"),
    ("Task Manager (when open)", r"airux_taskmgr", "Python"),
    ("KDE Plasma shell", r"/usr/bin/plasmashell", "C++"),
    ("KWin", r"kwin_wayland", "C++"),
]

PROBE = r'''
import json, os, re, sys, time
comps = json.loads(sys.argv[1]); n = int(sys.argv[2]); every = float(sys.argv[3])
TCK = os.sysconf("SC_CLK_TCK"); PAGE = os.sysconf("SC_PAGE_SIZE")
def procs():
    out = {}
    for d in os.listdir("/proc"):
        if not d.isdigit(): continue
        try:
            cmd = open(f"/proc/{d}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace")
            st = open(f"/proc/{d}/stat").read(); r = st.rindex(")"); f = st[r+2:].split()
            ticks = int(f[11]) + int(f[12])
            rss = int(open(f"/proc/{d}/statm").read().split()[1]) * PAGE
            pss = 0
            try:
                for l in open(f"/proc/{d}/smaps_rollup"):
                    if l.startswith("Pss:"): pss = int(l.split()[1]) * 1024
            except OSError: pass
            out[int(d)] = (cmd, ticks, rss, pss)
        except (OSError, ValueError, IndexError): pass
    return out
def total_busy():
    v = [int(x) for x in open("/proc/stat").readline().split()[1:9]]
    return sum(v) - v[3] - v[4], sum(v)
def meminfo():
    m = {l.split(":")[0]: int(l.split()[1]) * 1024 for l in open("/proc/meminfo")}
    return m["MemTotal"] - m["MemAvailable"]
samples = {c[0]: [] for c in comps}; cpu_tot = []; used = []
prev = procs(); pb = total_busy(); t0 = time.time()
for i in range(n):
    time.sleep(every)
    cur = procs(); b = total_busy(); dt = time.time() - t0; t0 = time.time()
    cpu_tot.append(100.0 * (b[0] - pb[0]) / max(b[1] - pb[1], 1) * os.cpu_count()); pb = b
    used.append(meminfo())
    for label, rx, _ in comps:
        rss = pss = cpu = 0.0; k = 0
        for pid, (cmd, ticks, r, p) in cur.items():
            if re.search(rx, cmd) and "measure" not in cmd:
                k += 1; rss += r; pss += p
                if pid in prev: cpu += 100.0 * (ticks - prev[pid][1]) / TCK / dt
        samples[label].append((k, rss, pss, cpu))
    prev = cur
res = {}
for label, s in samples.items():
    if not s or max(x[0] for x in s) == 0: res[label] = None; continue
    res[label] = {"procs": max(x[0] for x in s), "rss_mib": round(sum(x[1] for x in s) / len(s) / 2**20, 1),
                  "pss_mib": round(sum(x[2] for x in s) / len(s) / 2**20, 1), "cpu_pct": round(sum(x[3] for x in s) / len(s), 1),
                  "cpu_max": round(max(x[3] for x in s), 1)}
print(json.dumps({"components": res, "cpu_total_pct": round(sum(cpu_tot) / len(cpu_tot), 1),
                  "cpu_total_max": round(max(cpu_tot), 1), "ram_used_mib": round(sum(used) / len(used) / 2**20)}))
'''


def sample(label: str, n: int = 12, every: float = 5.0) -> dict:
    comps = json.dumps(COMPONENTS)
    sh("cat > /tmp/measure_probe.py <<'PY'\n" + PROBE + "\nPY")
    out = sh(f"python3 /tmp/measure_probe.py '{comps}' {n} {every}", timeout=n * every + 120)["out"]
    data = json.loads(out.strip().splitlines()[-1])
    print(f"== {label}: CPU {data['cpu_total_pct']}% of one core (max {data['cpu_total_max']}), RAM used {data['ram_used_mib']} MiB",
          flush=True)
    return data


def main() -> int:
    if "--boot" in sys.argv:
        vm.stop(D)
        time.sleep(2)
        for f in ("measure.qcow2", "OVMF_VARS.fd", "audio.wav"):
            (D / f).unlink(missing_ok=True)
        vm.start(D, p8.ISO, D / "measure.qcow2", "cd", mem=6656)
        print(f"booted; guest agent after {p8.wait_agent():.0f}s", flush=True)
    env = ""
    for _ in range(90):
        env = dc.session_env()
        if env:
            break
        time.sleep(5)
    cfg = sh("nproc; grep MemTotal /proc/meminfo")["out"].split()
    print(f"VM: {cfg[0]} vCPUs, {int(cfg[2]) // 1024} MiB RAM", flush=True)
    for _ in range(60):                       # AIRA ready: the model answers health
        if '"ok"' in sh("curl -s -m 5 127.0.0.1:8080/health")["out"]:
            break
        time.sleep(5)
    dc.as_user(env, "setsid neox >/dev/null 2>&1 < /dev/null &", 30)     # NEOX starts its native host
    print("AIRA ready; NEOX open; idling 5 minutes", flush=True)
    time.sleep(300)
    r = {"vm": {"vcpus": int(cfg[0]), "ram_mib": int(cfg[2]) // 1024}}
    r["idle"] = sample("idle")

    # one AIRA question, sampled while she answers
    tok = dc.as_user(env, "cat ~/.local/state/aira/shell.token")["out"].split()
    dc.as_user(env, f"curl -s -m 10 -H 'X-AIRA-Token: {tok[1]}' -H 'Content-Type: application/json' "
                    f"--data '{{\"text\": \"How much free disk space do I have in my home folder?\"}}' "
                    f"http://127.0.0.1:{tok[0]}/command")
    r["aira"] = sample("AIRA answering", n=8, every=3.0)

    dc.as_user(env, "systemd-run --user --collect --unit=measure-scan phagos scan --quick", 30)
    r["scan"] = sample("Phagos quick scan", n=10, every=3.0)
    r["scan_done"] = dc.as_user(env, "phagos status 2>/dev/null | head -20")["out"][-600:]

    sh("systemctl stop aira-llm; sleep 5")
    r["nomodel"] = sample("model stopped", n=4, every=3.0)
    sh("systemctl start aira-llm")
    out = Path("/srv/airux-work/build/vm/resources.json")
    out.write_text(json.dumps(r, indent=1))

    print("\n| Component | Language | Processes | RSS MiB | PSS MiB | CPU % idle | CPU % AIRA answering | CPU % Phagos scan |")
    print("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for label, _, lang in COMPONENTS:
        i, a, s = (r[k]["components"].get(label) for k in ("idle", "aira", "scan"))
        if not (i or a or s):
            continue
        x = i or a or s
        print(f"| {label} | {lang} | {x['procs']} | {x['rss_mib']} | {x['pss_mib']} | {i['cpu_pct'] if i else '-'} | "
              f"{a['cpu_pct'] if a else '-'} | {s['cpu_pct'] if s else '-'} |")
    print(f"\nWhole VM: CPU idle {r['idle']['cpu_total_pct']}%, AIRA answering {r['aira']['cpu_total_pct']}% "
          f"(max {r['aira']['cpu_total_max']}), Phagos scan {r['scan']['cpu_total_pct']}% (max {r['scan']['cpu_total_max']}); "
          f"RAM used {r['idle']['ram_used_mib']} MiB with the model, {r['nomodel']['ram_used_mib']} MiB without (CPU % is of one core; "
          f"{r['vm']['vcpus']} cores = {r['vm']['vcpus'] * 100}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
