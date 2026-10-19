"""The capability manifest — context engineering for the other side.

The chat model on the far side of the clipboard can only use what it
knows exists. The manifest is the discovery mechanism: a compact list
of the "tools" it may invoke, each with the block shape that invokes
it. The core generates the manifest for the built-in tools; settings
may declare more; and HOOKS may register additional tools at runtime
(hookSpecificOutput.registeredTools) — a registered tool appears in
the manifest from then on, which is how the model "gains capabilities"
over the clipboard transport.

The manifest ships in AGENTS.md (the bootstrap copy) and is refreshed
into result blocks whenever it changes mid-session: each bound
session gets the new manifest in its next result block, exactly once
per change.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

BUILTIN_TOOLS: list[dict] = [
    {
        "name": "shell.run",
        "description": "Run shell commands; output returns in a "
                       "```result block.",
        "shape": "```shell fence; header lines session:/action:/cwd:/"
                 "timeout:, then the commands",
    },
    {
        "name": "file.write",
        "description": "Propose a full-file create/update/delete. "
                       "Staged as a candidate; applied only after "
                       "human review.",
        "shape": "```file fence; first line `path: <path>`, optional "
                 "`op: create|update|delete`, then the FULL content",
    },
    {
        "name": "patch.apply",
        "description": "Propose a change to one file as a unified "
                       "diff. Staged and reviewed like file.write.",
        "shape": "```patch fence; first line `path: <path>`, then the "
                 "unified diff",
    },
    {
        "name": "session.cancel",
        "description": "Cancel the currently running run for a "
                       "session (or the last run).",
        "shape": "single line ```cancel``` optionally with id=<session>",
    },
    {
        "name": "session.rerun",
        "description": "Re-run the last payload of a session (or the "
                       "last run).",
        "shape": "single line ```rerun``` optionally with id=<session>",
    },
    {
        "name": "goal.set",
        "description": "Set the session's active goal (shown back in "
                       "every bound result block).",
        "shape": "```shell fence with header `action: goal-set` and "
                 "`session: <id>`; body = the goal title",
    },
    {
        "name": "task.add",
        "description": "Add open tasks to the session (shown back in "
                       "every bound result block).",
        "shape": "```shell fence with header `action: task-add` and "
                 "`session: <id>`; one task per body line",
    },
    {
        "name": "task.update",
        "description": "Move a task's status (open/doing/done).",
        "shape": "```shell fence with header `action: task-doing` / "
                 "`task-done` / `task-open` and `session: <id>`; "
                 "body = the task title",
    },
]


def render_manifest(tools: list[dict]) -> str:
    lines = ["— capabilities:"]
    for tool in tools:
        line = f"  {tool['name']} — {tool.get('description', '')}"
        if tool.get("shape"):
            line += f" [{tool['shape']}]"
        lines.append(line)
    return "\n".join(lines)


def manifest_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class ManifestStore:
    """Registered tools + per-session last-sent manifest hashes,
    persisted under the state dir so registrations survive restarts."""

    def __init__(self, state_dir: Path | str):
        self.dir = Path(state_dir).expanduser()
        self.tools_path = self.dir / "manifest-tools.json"
        self.sent_path = self.dir / "manifest-sent.json"

    # -- tool registry ----------------------------------------------------

    def register(self, tools: list[dict]) -> bool:
        """Add hook-registered tools. Returns True when the set changed."""
        current = self._registered()
        changed = False
        for tool in tools:
            if isinstance(tool, dict) and tool.get("name"):
                entry = {"name": str(tool["name"]),
                         "description": str(tool.get("description", "")),
                         "shape": str(tool.get("shape", ""))}
                if current.get(entry["name"]) != entry:
                    current[entry["name"]] = entry
                    changed = True
        if changed:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.tools_path.write_text(
                json.dumps(current, indent=2), encoding="utf-8")
        return changed

    def _registered(self) -> dict[str, dict]:
        try:
            data = json.loads(self.tools_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def all_tools(self, settings_tools: list[dict]) -> list[dict]:
        merged: dict[str, dict] = {t["name"]: t for t in BUILTIN_TOOLS}
        for tool in settings_tools:
            merged[tool["name"]] = tool
        for name, tool in self._registered().items():
            merged[name] = tool
        return list(merged.values())

    def current(self, settings_tools: list[dict]) -> tuple[str, str]:
        text = render_manifest(self.all_tools(settings_tools))
        return text, manifest_hash(text)

    # -- per-session refresh tracking ---------------------------------------

    def pending_section(self, binding_id: str,
                        settings_tools: list[dict]) -> str:
        """The manifest text when it changed for this session, else "".

        Marks the current hash as sent — the refresh rides exactly one
        result block per change.
        """
        text, digest = self.current(settings_tools)
        sent = self._sent()
        if sent.get(binding_id) == digest:
            return ""
        sent[binding_id] = digest
        self.dir.mkdir(parents=True, exist_ok=True)
        self.sent_path.write_text(json.dumps(sent, indent=2),
                                  encoding="utf-8")
        return text

    def _sent(self) -> dict[str, str]:
        try:
            data = json.loads(self.sent_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}
