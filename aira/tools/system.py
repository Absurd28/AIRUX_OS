"""System agent tools: machine info, disks, GPU, services, packages, network."""
from __future__ import annotations

import os
import platform
import re
import shutil
import time
from pathlib import Path

from .base import I, Registry, Risk, S, Tool, ToolError, exists_path, expand, privileged, run, truncate

SERVICE_RE = re.compile(r"^[A-Za-z0-9@._:\-]+$")
PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.\-]+$")


def system_info() -> dict:
    mem = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        if k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            mem[k] = int(v.split()[0]) * 1024
    os_release = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            os_release[k] = v.strip('"')
    uptime_s = float(Path("/proc/uptime").read_text().split()[0])
    return {
        "os": os_release.get("PRETTY_NAME", "unknown"),
        "kernel": platform.release(),
        "hostname": platform.node(),
        "uptime_hours": round(uptime_s / 3600, 1),
        "cpu_threads": os.cpu_count(),
        "load_average_1_5_15": [round(x, 2) for x in os.getloadavg()],
        "memory_total_gib": round(mem["MemTotal"] / 2**30, 2),
        "memory_available_gib": round(mem["MemAvailable"] / 2**30, 2),
        "swap_total_gib": round(mem.get("SwapTotal", 0) / 2**30, 2),
    }


def disk_usage(path: str = "~") -> dict:
    p = expand(path)
    if not p.exists():
        raise ToolError(f"path does not exist: {p}")
    u = shutil.disk_usage(p)
    return {"path": str(p), "total_gib": round(u.total / 2**30, 2), "used_gib": round(u.used / 2**30, 2),
            "free_gib": round(u.free / 2**30, 2), "used_percent": round(100 * u.used / u.total, 1)}


def gpu_status() -> dict:
    exe = shutil.which("nvidia-smi") or ("/usr/lib/wsl/lib/nvidia-smi"
                                         if Path("/usr/lib/wsl/lib/nvidia-smi").exists() else None)
    if not exe:
        raise ToolError("no NVIDIA GPU tools found (nvidia-smi missing)")
    out = run([exe, "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw",
               "--format=csv,noheader,nounits"])
    gpus = []
    for line in out.strip().splitlines():
        name, temp, util, used, total, power = (x.strip() for x in line.split(","))
        gpus.append({"name": name, "temperature_c": temp, "utilization_percent": util,
                     "memory_used_mib": used, "memory_total_mib": total, "power_w": power})
    return {"gpus": gpus}


def list_services(pattern: str = "", state: str = "running") -> dict:
    cmd = ["systemctl", "list-units", "--type=service", "--no-pager", "--no-legend", "--plain"]
    if state != "all":
        cmd.append(f"--state={state}")
    out = run(cmd)
    rows = []
    for line in out.strip().splitlines():
        parts = line.split(None, 4)
        if len(parts) >= 4 and (not pattern or pattern.lower() in parts[0].lower()):
            rows.append({"unit": parts[0], "active": parts[2], "sub": parts[3],
                         "description": parts[4] if len(parts) > 4 else ""})
    return {"count": len(rows), "services": rows[:60]}


def _check_service(name: str) -> str:
    if not SERVICE_RE.match(name):
        raise ToolError(f"invalid service name: {name!r}")
    return name if "." in name else name + ".service"


def service_status(name: str) -> dict:
    unit = _check_service(name)
    import subprocess
    r = subprocess.run(["systemctl", "show", unit, "--no-pager",
                        "--property=LoadState,ActiveState,SubState,Description,MainPID,ActiveEnterTimestamp"],
                       capture_output=True, text=True, timeout=20)
    props = dict(line.split("=", 1) for line in r.stdout.strip().splitlines() if "=" in line)
    if props.get("LoadState") == "not-found":
        raise ToolError(f"no such service: {unit}")
    return {"unit": unit, **props}


def service_control(name: str, action: str) -> dict:
    unit = _check_service(name)
    service_status(unit)  # must exist
    run(privileged(["systemctl", action, unit]), timeout=90)
    return service_status(unit)


def install_package(name: str) -> dict:
    if not PACKAGE_RE.match(name):
        raise ToolError(f"invalid package name: {name!r}")
    run(["apt-cache", "show", name], timeout=30)  # fails plainly if the package does not exist
    run(privileged(["apt-get", "install", "-y", "--no-install-recommends", name]), timeout=900)
    ver = run(["dpkg-query", "-W", "-f=${Version}", name]).strip()
    return {"package": name, "installed_version": ver}


def network_status() -> dict:
    addrs = run(["ip", "-brief", "address"])
    route = run(["ip", "route", "show", "default"])
    dns = Path("/etc/resolv.conf").read_text() if Path("/etc/resolv.conf").exists() else ""
    return {"interfaces": addrs.strip().splitlines(), "default_route": route.strip(),
            "dns_servers": re.findall(r"^nameserver\s+(\S+)", dns, re.M)}


def service_exists(name: str) -> bool:
    if not SERVICE_RE.match(name):
        return False
    import subprocess
    unit = name if "." in name else name + ".service"
    r = subprocess.run(["systemctl", "show", unit, "--property=LoadState", "--value"],
                       capture_output=True, text=True, timeout=20)
    return r.stdout.strip() not in ("not-found", "")


def package_known(name: str) -> bool:
    import subprocess
    if not PACKAGE_RE.match(name):
        return False
    return subprocess.run(["apt-cache", "show", name], capture_output=True, timeout=30).returncode == 0


def _service_readback(args: dict, result) -> tuple[bool, str]:
    st = service_status(args["name"])
    want = "inactive" if args["action"] == "stop" else "active"
    return st.get("ActiveState") == want, f"{st['unit']} is {st.get('ActiveState')} (expected {want})"


def _package_readback(args: dict, result) -> tuple[bool, str]:
    import subprocess
    r = subprocess.run(["dpkg-query", "-W", "-f=${Status}", args["name"]], capture_output=True, text=True)
    ok = "install ok installed" in r.stdout
    return ok, f"{args['name']} is {'installed' if ok else 'not installed'}"


def register(reg: Registry) -> None:
    A = "system"
    reg.add(Tool("system_info", A, "OS, kernel, uptime, CPU load and memory of this machine",
                 {}, [], system_info))
    reg.add(Tool("disk_usage", A, "total/used/free space of the disk holding a path",
                 {"path": S("a path on the disk, e.g. ~ or /")}, [], disk_usage,
                 entities=lambda a: [exists_path(a["path"])] if a.get("path") else []))
    reg.add(Tool("gpu_status", A, "GPU name, temperature, utilisation, memory, power", {}, [], gpu_status))
    reg.add(Tool("list_services", A, "list systemd services, optionally filtered by name",
                 {"pattern": S("substring of the service name"),
                  "state": S("which services", enum=["running", "failed", "all"])}, [], list_services))
    reg.add(Tool("service_status", A, "status of one systemd service", {"name": S("service name")},
                 ["name"], service_status,
                 entities=lambda a: [("service", a["name"], service_exists(a["name"]))]))
    reg.add(Tool("service_control", A, "start, stop or restart a systemd service (needs root)",
                 {"name": S("service name"), "action": S("what to do", enum=["start", "stop", "restart"])},
                 ["name", "action"], service_control, frozenset({Risk.ROOT}),
                 entities=lambda a: [("service", a["name"], service_exists(a["name"]))],
                 readback=_service_readback, changes_state=True))
    reg.add(Tool("install_package", A, "install a Debian package with apt (needs root and network)",
                 {"name": S("exact Debian package name")}, ["name"], install_package,
                 frozenset({Risk.INSTALL, Risk.ROOT}),
                 entities=lambda a: [("package", a["name"], package_known(a["name"]))],
                 readback=_package_readback, changes_state=True))
    reg.add(Tool("network_status", A, "network interfaces, addresses, default route, DNS servers",
                 {}, [], network_status))
