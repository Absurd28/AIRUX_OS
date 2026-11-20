"""Tor as NEOX's VPN: a dedicated Tor instance (separate from the private window's), with exit
countries chosen through its control socket.

  only NEOX      NEOX uses its SOCKS port (127.0.0.1:9060)
  whole computer nftables sends all TCP to its TransPort and DNS to its DNSPort (see system.py)
"""
from __future__ import annotations

import os
import pwd
import socket
import subprocess
import time
from pathlib import Path

SOCKS_PORT, TRANS_PORT, DNS_PORT = 9060, 9061, 5354
VIRTUAL_NET = "10.192.0.0/10"
# countries with plenty of Tor exits (others work too, but circuits may be slow or fail)
COUNTRIES = ["us", "de", "nl", "fr", "ca", "gb", "se", "ch", "fi", "no", "at", "ro", "pl", "cz", "es", "it", "jp", "sg"]


class TorError(Exception):
    pass


def torrc(data: Path, control: Path, country: str | None, system: bool) -> str:
    lines = [f"DataDirectory {data}", f"SocksPort 127.0.0.1:{SOCKS_PORT}", f"ControlSocket {control}",
             "CookieAuthentication 1", "User debian-tor", "Log notice stdout", "AvoidDiskWrites 1"]
    if system:
        lines += [f"TransPort 127.0.0.1:{TRANS_PORT}", f"DNSPort 127.0.0.1:{DNS_PORT}",
                  "AutomapHostsOnResolve 1", f"VirtualAddrNetworkIPv4 {VIRTUAL_NET}"]
    if country:
        lines += [f"ExitNodes {{{country.lower()}}}", "StrictNodes 1"]
    return "\n".join(lines) + "\n"


class Control:
    """Tor's control protocol (enough of it): authenticate with the cookie, SETCONF, SIGNAL, GETINFO."""

    def __init__(self, path: Path, cookie: Path, timeout: float = 15):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.settimeout(timeout)
        self.s.connect(str(path))
        self.f = self.s.makefile("rb")
        self.cmd(f"AUTHENTICATE {cookie.read_bytes().hex()}")

    def cmd(self, line: str) -> list[str]:
        self.s.sendall((line + "\r\n").encode())
        out = []
        while True:
            r = self.f.readline().decode(errors="replace").rstrip("\r\n")
            if not r:
                raise TorError("Tor closed the control connection")
            out.append(r[4:])
            if r[3:4] == " ":
                if not r.startswith("250"):
                    raise TorError(f"Tor said: {r}")
                return out

    def close(self) -> None:
        try:
            self.s.close()
        except OSError:
            pass


class TorBackend:
    def __init__(self, state: Path, run: Path):
        self.data = state / "tor"
        self.run = run / "tor"                    # Tor wants its control socket's folder to be its own
        self.control_path = self.run / "control"
        self.proc: subprocess.Popen | None = None

    def start(self, country: str | None, system: bool, timeout: float = 120) -> None:
        self.stop()
        tor_user = pwd.getpwnam("debian-tor")
        self.data.mkdir(parents=True, exist_ok=True)
        os.chown(self.data, tor_user.pw_uid, tor_user.pw_gid)
        os.chmod(self.data, 0o700)
        self.run.mkdir(parents=True, exist_ok=True)
        os.chown(self.run, tor_user.pw_uid, tor_user.pw_gid)
        os.chmod(self.run, 0o700)
        rc = self.run.parent / "torrc"
        rc.write_text(torrc(self.data, self.control_path, country, system))
        with open(self.run.parent / "tor.log", "w") as logf:
            self.proc = subprocess.Popen(["tor", "-f", str(rc)], stdout=logf, stderr=subprocess.STDOUT,
                                         start_new_session=True)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                raise TorError(f"Tor stopped while starting{self._reason()}")
            try:
                c = Control(self.control_path, self.data / "control_auth_cookie")
                phase = c.cmd("GETINFO status/bootstrap-phase")[0]
                c.close()
                if "PROGRESS=100" in phase:
                    return
            except (OSError, TorError):
                pass
            time.sleep(1)
        reason = self._reason()
        self.stop()
        raise TorError(f"Tor did not connect within two minutes{reason}")

    def _reason(self) -> str:
        try:
            lines = (self.run.parent / "tor.log").read_text(errors="replace").splitlines()
        except OSError:
            return ""
        # the first warnings say why (the final error is only "Reading config failed")
        bad = [l for l in lines if "[warn]" in l] or [l for l in lines if "[err]" in l]
        return f" ({bad[0].split('] ', 1)[-1][:160]})" if bad else ""

    def set_country(self, country: str | None) -> None:
        c = Control(self.control_path, self.data / "control_auth_cookie")
        try:
            if country:
                c.cmd(f"SETCONF ExitNodes={{{country.lower()}}} StrictNodes=1")
            else:
                c.cmd("RESETCONF ExitNodes StrictNodes")
            c.cmd("SIGNAL NEWNYM")                  # new circuits, through the new country
        finally:
            c.close()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None
