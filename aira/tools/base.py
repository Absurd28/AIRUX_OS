"""Tool definitions, risk classes and the registry.

A tool's risk is computed in code from its definition and the actual arguments (for example
write_file on an existing path is an overwrite). The model never decides risk.
Handlers are only reachable through the permission broker (aira.broker).
"""
from __future__ import annotations

import enum
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

MAX_TOOLS_PER_AGENT = 8
AGENTS = ("system", "desktop", "dev", "research", "assistant")


class Risk(str, enum.Enum):
    READ = "read"                    # no change to anything
    WRITE = "write"                  # creates or changes something reversibly (new file, git commit)
    DELETE = "delete"
    OVERWRITE = "overwrite"
    INSTALL = "install"
    NETWORK_CHANGE = "network_change"
    SEND_OFF = "send_off_machine"    # any data leaves the machine (includes search queries)
    ROOT = "root"
    EXEC = "exec"                    # arbitrary command chosen by the model


# CLAUDE.md: delete, overwrite, install, network changes, sending anything off the machine,
# and anything needing root require confirmation. Arbitrary commands too: they can do any of these.
CONFIRM = frozenset({Risk.DELETE, Risk.OVERWRITE, Risk.INSTALL, Risk.NETWORK_CHANGE,
                     Risk.SEND_OFF, Risk.ROOT, Risk.EXEC})


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "data": self.data} if self.ok else {"ok": False, "error": self.error}


class ToolError(Exception):
    """Raised by handlers for expected failures; reported to the user plainly."""


@dataclass
class Tool:
    name: str
    agent: str
    description: str
    params: dict                              # JSON schema "properties"
    required: list[str]
    handler: Callable[..., Any]
    risk: Callable[[dict], set[Risk]] | frozenset[Risk] = frozenset({Risk.READ})

    def risks(self, args: dict) -> set[Risk]:
        r = self.risk(args) if callable(self.risk) else self.risk
        return set(r) or {Risk.READ}

    def needs_confirmation(self, args: dict) -> bool:
        return bool(self.risks(args) & CONFIRM)

    def args_schema(self) -> dict:
        return {"type": "object", "properties": self.params, "required": self.required,
                "additionalProperties": False}

    def signature(self) -> str:
        ps = ", ".join(f"{k}{'' if k in self.required else '?'}" for k in self.params)
        return f"{self.name}({ps}): {self.description}"


@dataclass
class Registry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def add(self, tool: Tool) -> Tool:
        if tool.name in self.tools:
            raise ValueError(f"duplicate tool {tool.name}")
        if tool.agent not in AGENTS:
            raise ValueError(f"unknown agent {tool.agent}")
        if len(self.for_agent(tool.agent)) >= MAX_TOOLS_PER_AGENT:
            raise ValueError(f"agent {tool.agent} already has {MAX_TOOLS_PER_AGENT} tools")
        self.tools[tool.name] = tool
        return tool

    def for_agent(self, agent: str) -> list[Tool]:
        return [t for t in self.tools.values() if t.agent == agent]

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)


# ---------- helpers shared by tool modules ----------

def S(desc: str, **extra) -> dict:
    return {"type": "string", "description": desc, **extra}


def I(desc: str, minimum: int | None = None, maximum: int | None = None) -> dict:
    d: dict = {"type": "integer", "description": desc}
    if minimum is not None:
        d["minimum"] = minimum
    if maximum is not None:
        d["maximum"] = maximum
    return d


def expand(path: str) -> Path:
    """Expand ~ and $VARS. Relative paths are relative to the user's home, never guessed.

    "~name" means user name's home; if no such user exists the path is rejected with a hint
    instead of being silently treated as a file called "~name".
    """
    raw = path.strip()
    if raw.startswith("~") and not raw.startswith("~/") and raw != "~":
        user = raw[1:].split("/", 1)[0]
        if os.path.expanduser("~" + user) == "~" + user:  # no such user
            raise ToolError(f"ambiguous path {raw!r}: there is no user {user!r}. "
                            f"For a path in the home folder write ~/{raw[1:]}")
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    return p if p.is_absolute() else Path.home() / p


def run(cmd: list[str], timeout: float = 60, cwd: str | Path | None = None,
        input_text: str | None = None) -> str:
    """Run a command without a shell. Raises ToolError with stderr on failure."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                           input=input_text)
    except FileNotFoundError as exc:
        raise ToolError(f"command not found: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"{cmd[0]} timed out after {timeout:.0f}s") from exc
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip()[-800:]
        raise ToolError(f"{' '.join(cmd[:3])} failed (exit {r.returncode}): {msg}")
    return r.stdout


def privileged(cmd: list[str]) -> list[str]:
    """Prefix sudo -n when not root: never prompts; fails plainly if not permitted."""
    return cmd if os.geteuid() == 0 else ["sudo", "-n", *cmd]


def truncate(text: str, limit: int = 4000) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n... [{len(text) - limit} more characters]"
