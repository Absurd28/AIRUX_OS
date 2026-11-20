"""phagosd's logic without the kernel: files written, scans, quarantine, the API's permissions, decoys,
and behaviour (a real harmless child process is paused, then killed or resumed by the user's choice).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from phagos import daemon as D
from phagos import fanotify as fa
from phagos.procmon import Activity, read

RULES = Path(__file__).resolve().parents[2] / "phagos" / "rules"
EICAR = r"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
ME = os.getuid()


@pytest.fixture
def ph(tmp_path):
    home = tmp_path / "home" / "u"
    for d in ("Documents", "Downloads", "Pictures"):
        (home / d).mkdir(parents=True)
    info = tmp_path / "dpkg"
    info.mkdir()
    sleep = shutil.which("sleep")
    (info / "coreutils.list").write_text(f"{sleep}\n{os.path.realpath(sleep)}\n")
    paths = D.Paths(state=tmp_path / "state", rules=(RULES,), socket=tmp_path / "s.sock", dpkg_info=str(info),
                    homes=(str(home),))
    p = D.Phagos(paths, seed=1, train=False)
    p.authorized = lambda peer: peer[1] == 0          # no polkit in tests: only "root" is an admin
    p.home = home
    return p


def written(ph, path: Path, pid: int | None = None):
    fd = os.open(path, os.O_RDONLY)
    try:
        ph.on_written(str(path), fd, pid or os.getpid() + 999999)
    finally:
        os.close(fd)


def test_test_file_written_to_downloads_is_quarantined(ph):
    f = ph.home / "Downloads" / "eicar.com"
    f.write_text(EICAR)
    written(ph, f)                     # the writer's pid is not Phagos's own (Phagos ignores its own writes)
    assert not f.exists()
    ev = ph.store.events(5, ("threat",))[0]
    assert ev["threat"] == "EICAR-Test-File" and "quarantined" in ev["action"]
    items = ph.quarantine.items()
    assert len(items) == 1 and items[0]["path"] == str(f)


def test_exec_of_a_blocklisted_program_is_denied_and_others_run(ph, tmp_path):
    good = tmp_path / "true-copy"
    shutil.copy(shutil.which("true"), good)
    import hashlib
    rules = tmp_path / "local"
    rules.mkdir()
    bad = tmp_path / "bad-tool"
    shutil.copy(shutil.which("true"), bad)
    with open(bad, "ab") as f:
        f.write(b"phagos-test-marker")                 # different hash from the good copy
    (rules / "test.sha256").write_text(hashlib.sha256(bad.read_bytes()).hexdigest() + " Test.Blocklisted\n")
    ph.rules = D.RuleSet.load(RULES, rules)
    ph.scanner.rules = ph.rules
    for path, expect in ((good, True), (bad, False)):
        fd = os.open(path, os.O_RDONLY)
        assert ph.on_exec(str(path), fd, 999999999) is expect
        os.close(fd)
    time.sleep(0.5)                                    # containment runs in a thread
    assert not bad.exists() and good.exists()


def test_scan_finds_and_reports(ph):
    (ph.home / "Downloads" / "x.php").write_text("<?php eval(base64_decode($_POST['x'])); ?>")
    (ph.home / "Downloads" / "notes.txt").write_text("nothing to see")
    job = ph.start_scan("custom", str(ph.home / "Downloads"), 0)
    for _ in range(50):
        if ph.scan_status()["done"]:
            break
        time.sleep(0.1)
    st = ph.scan_status()
    assert st["done"] and st["scanned"] >= 2
    assert [f["threat"] for f in st["found"]] == ["Webshell.PHP"]
    with pytest.raises(D.ApiError, match="does not exist"):
        ph.start_scan("custom", "/no/such/place", 0)


def test_api_permissions(ph):
    f = ph.home / "Downloads" / "eicar.txt"
    f.write_text(EICAR)
    ph.contain(ph.verdict(str(f)), ME, "test")
    qid = ph.quarantine.items()[0]["id"]
    user, root = (os.getpid(), 1000, 1000), (os.getpid(), 0, 0)
    assert ph.api({"method": "status"}, user)["realtime"] is True
    with pytest.raises(D.ApiError, match="administrator"):
        ph.api({"method": "quarantine_restore", "params": {"id": qid}}, user)
    with pytest.raises(D.ApiError, match="administrator"):
        ph.api({"method": "realtime", "params": {"on": False}}, user)
    out = ph.api({"method": "quarantine_restore", "params": {"id": qid}}, root)
    assert f.exists() and out["restored"]["threat"] == "EICAR-Test-File"
    # restored means allowed: the same file is no longer a threat
    assert ph.verdict(str(f)).engine == "allowed"
    with pytest.raises(D.ApiError, match="unknown method"):
        ph.api({"method": "rm_rf"}, root)


def test_decoys_notice_tampering(ph):
    assert ph.place_decoys() == 3
    assert ph.check_decoys() == []
    decoy = ph.home / "Documents" / ".phagos-decoy.docx"
    decoy.write_bytes(os.urandom(4096))
    assert ph.check_decoys() == [str(decoy)]


def busy_child(exe: str) -> subprocess.Popen:
    return subprocess.Popen([exe, "60"])


def state(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]


def ransomware_like(ph, pid: int, ticks: int = 8):
    p = read(pid)
    out = []
    for i in range(ticks):
        ph.activity[p.key] = Activity(writes=40, hot=40, paths=["/home/u/Documents/a.locked"])
        out += ph.tick(now=time.monotonic() + i * 2, only={pid})
    return out


def test_dangerous_behaviour_is_paused_then_killed(ph, tmp_path):
    exe = tmp_path / "not-from-a-package"                 # a copy of sleep: not package-owned
    shutil.copy(shutil.which("sleep"), exe)
    child = busy_child(str(exe))
    try:
        time.sleep(0.2)
        flagged = ransomware_like(ph, child.pid)
        assert len(flagged) == 1 and flagged[0].pid == child.pid
        assert state(child.pid) == "T"                      # stopped
        ev = ph.store.events(5, ("behavior",))[0]
        assert ev["action"] == "paused" and ev["engine"] == "dca"
        with pytest.raises(D.ApiError, match="another user"):
            ph.decide(flagged[0].event_id, "kill", ME + 1 if ME else 4242)
        out = ph.decide(flagged[0].event_id, "kill", ME)
        assert out["action"] == "stopped"
        child.wait(timeout=5)
        assert child.returncode == -9
    finally:
        if child.poll() is None:
            child.kill()


def test_allowed_behaviour_resumes_and_is_left_alone(ph, tmp_path):
    exe = tmp_path / "my-own-tool"
    shutil.copy(shutil.which("sleep"), exe)
    child = busy_child(str(exe))
    try:
        time.sleep(0.2)
        flagged = ransomware_like(ph, child.pid)
        ph.decide(flagged[0].event_id, "allow", ME)
        time.sleep(0.1)
        assert state(child.pid) in ("S", "R")
        assert ransomware_like(ph, child.pid) == []         # not asked again
    finally:
        child.kill()


def test_a_packaged_tool_doing_the_same_is_not_paused(ph):
    child = busy_child(shutil.which("sleep"))              # owned by "coreutils" in this fake dpkg
    try:
        time.sleep(0.2)
        assert ransomware_like(ph, child.pid, ticks=12) == []
        assert state(child.pid) in ("S", "R")
    finally:
        child.kill()


def test_fanotify_records_parse():
    raw = fa.METADATA.pack(24, 3, 0, 24, fa.FAN_OPEN_EXEC_PERM, 7, 1234) + \
        fa.METADATA.pack(24, 3, 0, 24, fa.FAN_CLOSE_WRITE, 8, 99)
    evs = fa.parse(raw)
    assert [(e.fd, e.pid, e.is_perm) for e in evs] == [(7, 1234, True), (8, 99, False)]
    assert fa.response(7, False) == fa.RESPONSE.pack(7, fa.FAN_DENY)
    with pytest.raises(fa.FanotifyError):
        fa.parse(fa.METADATA.pack(24, 2, 0, 24, 0, 1, 1))
