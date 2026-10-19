"""Settings: user scope + project scope, merged like Claude Code's.

  user scope:    ~/.config/clip-harness/settings.json
  project scope: ./.clip-harness/settings.json

Merge rule (mirrors Claude Code settings merging): hook handler lists are
concatenated (user first, then project); list settings like allowlist are
concatenated too; scalar settings are overridden by the project scope.

Settings schema (all fields optional):

{
  "state_dir": "~/.local/state/clip-harness",
  "timeout": 120,                    // per-command seconds
  "allowlist": ["git status", "ls *"],
  "denylist": ["sudo *"],
  "protected_paths": [".env", "*.key", "~/.ssh"],
  "hard_block_patterns": ["rm -rf /", ":(){ :|:& };:"],
  "mutation_gate": "review",         // review | allow | deny
  "shell_sandbox": "optional",       // optional | strict — optional
                                     // lets a reviewer accept a held
                                     // shell command UNSANDBOXED;
                                     // strict removes that option, so a
                                     // reviewed command can only run
                                     // under the Seatbelt sandbox
  "result_budget_chars": 100000,     // output chars per result block
                                     // before head+tail elision (the
                                     // full output is kept on disk)
  "color": "auto",                   // auto | always | never — terminal
                                     // diff colors (rendering only;
                                     // NO_COLOR env also forces off)
  "hooks": {
    "PreRun": [
      {"matcher": "Bash", "command": "~/hooks/check.sh", "timeout": 10}
    ],
    "PostRun": [
      {"matcher": "*", "command": "~/hooks/log.sh"}
    ]
  }
}
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .core import (DEFAULT_HOOK_TIMEOUT, DEFAULT_RESULT_BUDGET_CHARS,
                    DEFAULT_TIMEOUT)

USER_SETTINGS = Path("~/.config/clip-harness/settings.json")
PROJECT_SETTINGS = Path(".clip-harness/settings.json")
DEFAULT_STATE_DIR = Path("~/.local/state/clip-harness")


@dataclass
class HookHandler:
    command: str
    matcher: str = "*"
    if_pattern: str = ""  # optional narrowing, matched against command text
    timeout: float = DEFAULT_HOOK_TIMEOUT

    @classmethod
    def from_dict(cls, data: dict) -> "HookHandler":
        return cls(
            command=str(data["command"]),
            matcher=str(data.get("matcher", "*")),
            if_pattern=str(data.get("if", "")),
            timeout=float(data.get("timeout", DEFAULT_HOOK_TIMEOUT)),
        )


@dataclass
class Settings:
    state_dir: Path = field(default_factory=lambda: DEFAULT_STATE_DIR.expanduser())
    timeout: float = DEFAULT_TIMEOUT
    allowlist: list[str] = field(default_factory=list)
    denylist: list[str] = field(default_factory=list)
    protected_paths: list[str] = field(default_factory=list)
    hard_block_patterns: list[str] = field(default_factory=list)
    mutation_gate: str = "review"  # review | allow | deny
    # How a held shell command may be approved at review: "optional"
    # (default) offers both sandboxed and unsandboxed accepts;
    # "strict" removes the unsandboxed option entirely. Gate-relevant
    # like mutation_gate — it changes what may run — so it IS part of
    # the config hash.
    shell_sandbox: str = "optional"  # optional | strict
    # Presentation only: how much command output a result block carries
    # before head+tail elision (the full output is always preserved in
    # the run's output file). Sized for the receiving surface — the
    # default leaves room inside a 128k-capped chat message. Never part
    # of the gate config hash, like color: it cannot change what runs.
    result_budget_chars: int = DEFAULT_RESULT_BUDGET_CHARS
    # Rendering only (terminal diff colors); never part of the gate
    # config hash — a color change is not a ConfigChange.
    color: str = "auto"  # auto | always | never
    hooks: dict[str, list[HookHandler]] = field(default_factory=dict)
    # Extra capability-manifest tools declared in settings (hooks may
    # also register tools at runtime via registeredTools).
    tools: list[dict] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)  # files actually loaded

    def hook_handlers(self, event_name: str) -> list[HookHandler]:
        return list(self.hooks.get(event_name, []))

    def config_hash(self) -> str:
        """Stable hash of the effective gate-relevant configuration.

        Used for ConfigChange detection: an allowlist or hook edit made
        while the harness is armed must show up in the evidence.
        """
        blob = json.dumps(
            {
                "allowlist": self.allowlist,
                "denylist": self.denylist,
                "protected_paths": self.protected_paths,
                "hard_block_patterns": self.hard_block_patterns,
                "mutation_gate": self.mutation_gate,
                "shell_sandbox": self.shell_sandbox,
                "hooks": {
                    event: [
                        {"matcher": h.matcher, "command": h.command,
                         "if": h.if_pattern, "timeout": h.timeout}
                        for h in handlers
                    ]
                    for event, handlers in sorted(self.hooks.items())
                },
            },
            sort_keys=True,
        )
        return hashlib.sha256(blob.encode()).hexdigest()


def _positive_int(value: object) -> int | None:
    """Parse a positive int from a settings value; None when unusable
    (absent, non-numeric, zero/negative, a bool) — the caller then keeps
    the current/default value. Fail safe, never crash, on bad config."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _load_json(path: Path) -> dict | None:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _merge(base: Settings, data: dict, source: str) -> None:
    base.sources.append(source)
    if "state_dir" in data:
        base.state_dir = Path(str(data["state_dir"])).expanduser()
    if "timeout" in data:
        base.timeout = float(data["timeout"])
    if "mutation_gate" in data:
        base.mutation_gate = str(data["mutation_gate"])
    shell_sandbox = data.get("shell_sandbox")
    if isinstance(shell_sandbox, str) \
            and shell_sandbox.lower() in ("optional", "strict"):
        base.shell_sandbox = shell_sandbox.lower()
    budget = _positive_int(data.get("result_budget_chars"))
    if budget is not None:
        base.result_budget_chars = budget
    color = data.get("color")
    if isinstance(color, str) and color.lower() in ("auto", "always",
                                                    "never"):
        base.color = color.lower()
    for key in ("allowlist", "denylist", "protected_paths", "hard_block_patterns"):
        if key in data and isinstance(data[key], list):
            getattr(base, key).extend(str(x) for x in data[key])
    tools = data.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict) and tool.get("name"):
                base.tools.append({
                    "name": str(tool["name"]),
                    "description": str(tool.get("description", "")),
                    "shape": str(tool.get("shape", "")),
                })
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for event, handlers in hooks.items():
            if isinstance(handlers, list):
                base.hooks.setdefault(str(event), []).extend(
                    HookHandler.from_dict(h) for h in handlers
                    if isinstance(h, dict) and "command" in h
                )


def load_settings(cwd: str | Path | None = None,
                  user_path: Path | None = None) -> Settings:
    """Load and merge user + project settings for `cwd`."""
    settings = Settings()
    user_file = (user_path or USER_SETTINGS).expanduser()
    data = _load_json(user_file)
    if data is not None:
        _merge(settings, data, str(user_file))
    project_file = Path(cwd or os.getcwd()) / PROJECT_SETTINGS
    data = _load_json(project_file)
    if data is not None:
        _merge(settings, data, str(project_file))
    return settings


def add_project_allowlist_pattern(cwd: str | Path, pattern: str) -> bool:
    """Persist an allowlist pattern into the PROJECT settings file
    (`.clip-harness/settings.json` under `cwd`) — the write behind a
    review's "always allow this command". Other keys are preserved;
    the pattern is appended to the file's own `allowlist` list (the
    merged settings concatenate scopes, so the project file's list is
    where a project-scoped approval belongs). Returns True when the
    file changed (False when the pattern was already there)."""
    path = Path(cwd) / PROJECT_SETTINGS
    data = _load_json(path) or {}
    allowlist = data.get("allowlist")
    if not isinstance(allowlist, list):
        allowlist = []
    if pattern in allowlist:
        return False
    allowlist.append(pattern)
    data["allowlist"] = allowlist
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True
