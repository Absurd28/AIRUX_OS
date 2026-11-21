"""AIRUX Task Manager: /proc parsing and grouping on a fake tree, performance counters, startup
apps, and the broker gating real actions on a real child process."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from airux_taskmgr import perf as PF
from airux_taskmgr import procs as P
from airux_taskmgr import startup as ST
from airux_taskmgr.actions import TaskManager

UID = os.getuid()


def stat_line(pid, comm, ppid, utime, stime, start, state="S", nice=0, threads=1):
    f = [state, ppid, pid, pid, 0, -1, 0, 0, 0, 0, 0, utime, stime, 0, 0, 20, nice, threads, 0, start, 1000, 50]
    return f"{pid} ({comm}) " + " ".join(str(x) for x in f) + "\n"


def make_proc(root: Path, pid: int, comm: str, cmd: str, uid: int, cgroup: str, ticks=(0, 0), ppid=1, start=100):
    d = root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "stat").write_text(stat_line(pid, comm, ppid, ticks[0], ticks[1], start))
    (d / "cmdline").write_text(cmd.replace(" ", "\0"))
    (d / "status").write_text(f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
    (d / "statm").write_text("1000 300 100 1 0 1 0\n")
    (d / "cgroup").write_text(f"0::{cgroup}\n")
    (d / "io").write_text("read_bytes: 1000\nwrite_bytes: 2000\n")


def write_stat(root: Path, total: int):
    (root / "stat").write_text(f"cpu  {total} 0 0 0 0 0 0 0 0 0\ncpu0 {total} 0 0 0 0 0 0 0 0 0\n")


@pytest.fixture
def fake(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    write_stat(proc, 1000)
    app_cg = f"/user.slice/user-{UID}.slice/user@{UID}.service/app.slice/app-org.kde.dolphin-4242.scope"
    make_proc(proc, 500, "dolphin", "/usr/bin/dolphin", UID, app_cg, (10, 0))
    make_proc(proc, 501, "kioworker", "/usr/lib/kioworker file", UID, app_cg, (5, 0), ppid=500)
    make_proc(proc, 600, "pipewire", "/usr/bin/pipewire", UID,
              f"/user.slice/user-{UID}.slice/user@{UID}.service/session.slice/pipewire.service")
    make_proc(proc, 700, "sshd", "/usr/sbin/sshd -D", UID + 1 if UID == 0 else 0, "/system.slice/ssh.service")
    make_proc(proc, 3, "kworker/0:0", "", 0, "/", ppid=2)          # a kernel thread
    apps = tmp_path / "apps"
    apps.mkdir()
    (apps / "org.kde.dolphin.desktop").write_text("[Desktop Entry]\nName=Dolphin\nIcon=system-file-manager\n"
                                                  "[Desktop Action x]\nName=Other\n")
    return proc, apps


def test_parse_stat_with_odd_name():
    st = P.parse_stat(stat_line(9, "a) (b c", 1, 7, 3, 55))
    assert st["comm"] == "a) (b c" and st["utime"] == 7 and st["stime"] == 3 and st["start"] == 55


@pytest.mark.parametrize("unit,expect", [
    ("app-org.kde.dolphin-4242.scope", "org.kde.dolphin"),
    ("app-org.kde.konsole@a1b2c3.service", "org.kde.konsole"),
    ("app-gnome-org.gnome.Terminal-99.scope", "org.gnome.Terminal"),
    ("app-aira\\x2dshell@x.service", "aira-shell"),
    ("pipewire.service", None),
])
def test_app_id_from_unit(unit, expect):
    assert P.app_id_from_unit(unit) == expect


def test_groups_and_cpu(fake):
    proc, apps = fake
    s = P.Sampler(str(proc), uid=UID, names=P.DesktopNames((str(apps),)))
    s.snapshot()
    write_stat(proc, 1100)                                          # 100 ticks pass
    make_proc(proc, 500, "dolphin", "/usr/bin/dolphin", UID,
              f"/user.slice/user-{UID}.slice/user@{UID}.service/app.slice/app-org.kde.dolphin-4242.scope", (30, 0))
    procs = s.snapshot()
    assert 3 not in [p.pid for p in procs]                          # kernel thread left out
    assert P.find(procs, 500).cpu == 20.0                           # 20 of 100 ticks
    groups = {g.key: g for g in s.groups(procs)}
    app = next(g for g in groups.values() if g.kind == "app")
    assert app.name == "Dolphin" and app.icon == "system-file-manager"
    assert sorted(p.pid for p in app.procs) == [500, 501]
    assert groups["pid:600"].kind == "background"
    assert groups["pid:700"].kind == "system"
    assert app.to_dict()["memory"] == 2 * 200 * P.PAGE


def test_parse_pmon():
    text = ("# gpu         pid   type     sm    mem    enc    dec    jpg    ofa     fb   ccpm    command\n"
            "# Idx           #    C/G      %      %      %      %      %      %     MB     MB    name\n"
            "    0       1234     G      7      2      -      -      -      -     45      0    kwin_wayland\n"
            "    0       5678     C     55     10      -      -      -      -   2100      0    llama-server\n")
    assert P.parse_pmon(text) == {1234: (7.0, 45), 5678: (55.0, 2100)}


def test_perf_counters(tmp_path):
    proc, sysr = tmp_path / "proc", tmp_path / "sys"
    (proc / "net").mkdir(parents=True)
    (sysr / "block" / "nvme0n1" / "queue").mkdir(parents=True)
    (sysr / "block" / "loop0").mkdir(parents=True)
    (sysr / "block" / "nvme0n1" / "queue" / "rotational").write_text("0\n")
    (sysr / "block" / "nvme0n1" / "size").write_text("1000\n")
    (proc / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 250 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
    (proc / "cpuinfo").write_text("model name\t: Test CPU\n")

    def counters(busy, idle, sect, net):
        (proc / "stat").write_text(f"cpu  {busy} 0 0 {idle} 0 0 0 0\ncpu0 {busy} 0 0 {idle} 0 0 0 0\n")
        (proc / "diskstats").write_text(f" 259 0 nvme0n1 1 0 {sect} 0 1 0 {sect} 0 0 {sect} 0\n 7 0 loop0 1 0 1 0 1 0 1 0 0 1 0\n")
        (proc / "net" / "dev").write_text("h1\nh2\n  eth0: " + f"{net} 0 0 0 0 0 0 0 {net} 0 0 0 0 0 0 0\n")
    counters(100, 100, 0, 0)
    p = PF.Perf(str(proc), str(sysr))
    p.sample()
    counters(150, 150, 2000, 1000)
    time.sleep(0.05)
    out = p.sample()
    assert out["cpu"]["util"] == 50.0 and out["cpu"]["model"] == "Test CPU"
    assert out["memory"]["used"] == 750 * 1024
    assert [d["name"] for d in out["disks"]] == ["nvme0n1"] and out["disks"][0]["ssd"]
    assert out["disks"][0]["read_bps"] > 0 and out["network"][0]["name"] == "eth0"
    assert len(out["cpu"]["history"]) == 2


def test_startup_disable_and_enable(tmp_path):
    sysd, user = tmp_path / "etc", tmp_path / "home"
    sysd.mkdir()
    (sysd / "foo.desktop").write_text("[Desktop Entry]\nName=Foo\nExec=foo\n")
    (sysd / "gnomeonly.desktop").write_text("[Desktop Entry]\nName=G\nExec=g\nOnlyShowIn=GNOME;\n")
    assert [e["id"] for e in ST.entries([sysd], user)] == ["foo.desktop"]
    e = ST.set_enabled("foo.desktop", False, [sysd], user)
    assert e["enabled"] is False and (user / "foo.desktop").read_text().count("Hidden=true") == 1
    e = ST.set_enabled("foo.desktop", True, [sysd], user)
    assert e["enabled"] is True and not (user / "foo.desktop").exists()     # our override removed
    with pytest.raises(ValueError):
        ST.target("../evil.desktop", user)


@pytest.fixture
def child():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield p
    if p.poll() is None:
        p.kill()
    p.wait()


def _tm(tmp_path, answer, asked):
    def confirm(ev):
        asked.append(ev)
        return answer
    tm = TaskManager(confirm, log_path=tmp_path / "log.jsonl")
    tm.tick()
    return tm


def test_end_task_needs_confirmation(tmp_path, child):
    asked = []
    tm = _tm(tmp_path, False, asked)
    p = tm.find(child.pid)
    out = tm.run("end_task", {"pid": child.pid, "start": p.start})
    assert not out["allowed"] and out["decision"].startswith("denied") and child.poll() is None
    assert asked and asked[0]["risks"] == ["stop"] and "Anything it has not saved is lost" in asked[0]["summary"]
    assert '"tool": "end_task"' in (tmp_path / "log.jsonl").read_text()


def test_end_task_confirmed_and_read_back(tmp_path, child):
    tm = _tm(tmp_path, True, [])
    p = tm.find(child.pid)
    out = tm.run("end_task", {"pid": child.pid, "start": p.start})
    assert out["ok"] and out["data"]["verified"], out
    assert child.wait(5) is not None


def test_end_task_wrong_start_time_is_refused(tmp_path, child):
    tm = _tm(tmp_path, True, [])
    out = tm.run("end_task", {"pid": child.pid, "start": 1})      # pid reuse guard
    assert not out["ok"] and "not running any more" in out["error"] and child.poll() is None


def test_lowering_priority_runs_without_asking(tmp_path, child):
    asked = []
    tm = _tm(tmp_path, False, asked)
    p = tm.find(child.pid)
    out = tm.run("set_priority", {"pid": child.pid, "start": p.start, "nice": 5})
    assert out["ok"] and out["data"]["verified"] and not asked


def test_risks_computed_in_code(tmp_path):
    tm = _tm(tmp_path, False, [])
    svc = tm.registry.get("service")
    assert svc.risks({"name": "ssh", "action": "stop", "scope": "system"}) == {"stop", "root"}
    assert svc.risks({"name": "x", "action": "start", "scope": "user"}) == {"write"}
    assert not svc.needs_confirmation({"name": "x", "action": "start", "scope": "user"})
    out = tm.run("service", {"name": "ssh", "action": "stop", "scope": "system"})
    assert not out["allowed"]
    out = tm.run("end_task", {"pid": "1", "start": 0})
    assert not out["allowed"] and "must be an integer" in out["decision"]
