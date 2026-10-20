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
                       "```result block. Oversized output is elided "
                       "there — the block names the file holding the "
                       "full output; page it, don't re-run.",
        "shape": "```shell fence; header lines session:/action:/cwd:/"
                 "timeout:/attend:/writes:/goal:/expect-revision:/"
                 "action-id:, then the commands",
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
        "name": "file.edit",
        "description": "Propose an exact-text edit to ONE existing "
                       "file: old_string must match the file exactly "
                       "(whitespace included) and be unique, unless "
                       "replace_all is set. Staged and reviewed like "
                       "file.write — the harness generates the diff, "
                       "so never hand-count patch hunks for a change "
                       "you can express as an edit.",
        "shape": "```edit fence; first line `path: <path>`, optional "
                 "`replace_all: true|false`, then an `old_string:` "
                 "section and a `new_string:` section with the exact "
                 "text (empty new_string deletes the matched block)",
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
        "description": "Set the active goal. Goals are DURABLE: the "
                       "first goal-set creates a goal with a "
                       "harness-issued goal_uid (returned in the "
                       "result) and binds the session to it; the goal "
                       "outlives the session and is shown back in "
                       "every bound result block.",
        "shape": "```shell fence with header `action: goal-set` and "
                 "`session: <id>`; body = the goal title",
    },
    {
        "name": "goal.list",
        "description": "List every durable goal (goal_uid, id, "
                       "status, revision, current plan step). "
                       "Read-only — use it to rediscover a goal "
                       "after losing the chat or session.",
        "shape": "```shell fence with header `action: goal-list` "
                 "(no body, no session needed)",
    },
    {
        "name": "goal.show",
        "description": "Show one durable goal in full: goal text, "
                       "plan + steps, tasks, backlog, latest result, "
                       "revision, provenance. Read-only.",
        "shape": "```shell fence with headers `action: goal-show` "
                 "and `goal: <goal_uid>` (or the session's bound "
                 "goal when no `goal:` header is given)",
    },
    {
        "name": "task.add",
        "description": "Add open tasks to the bound durable goal "
                       "(shown back in every bound result block).",
        "shape": "```shell fence with header `action: task-add` and "
                 "`session: <id>`; one task per body line",
    },
    {
        "name": "task.update",
        "description": "Change task statuses in ONE atomic batch: "
                       "every transition applies, or none does. "
                       "Guard it with `expect-revision:` (the goal's "
                       "current revision) and make it replay-safe "
                       "with a unique `action-id:`.",
        "shape": "```shell fence with header `action: task-update` "
                 "and `session: <id>`; one `<status>: <exact task "
                 "title>` line per transition, status done/doing/open",
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
