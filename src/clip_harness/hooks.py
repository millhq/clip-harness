"""Hook dispatch: Claude Code's contract, copied deliberately.

One dispatcher runs the user-configured command handlers for a lifecycle
event. Each handler gets ONE JSON object on stdin and may print AT MOST one
JSON object on stdout; diagnostics belong on stderr.

Exit-code semantics (Claude Code's, per the hooks reference):
  exit 0            proceed; stdout JSON may still deny/ask/rewrite
  exit 2            hard block — evaluated before allowlist rules;
                    stderr is the reason. Wins even over a JSON "allow".
  anything else,    hook error (crash, timeout, unparseable output)
  a crash, bad JSON

THE DELIBERATE DEVIATION (report section 7.2): in Claude Code a failed
PreToolUse hook fails OPEN unless onFailure:"block" is set. Here, GATE
hooks (PayloadCaptured, PreRun, ReviewDecision, RunClosed) fail CLOSED —
a gate hook that errors means the payload does not run, with the reason
in the receipt. Observational hooks (SessionStart, PostRun,
PostRunFailure, SessionEnd, ConfigChange) fail open.
"""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any

from .contract import validate_hook_input, validate_hook_output
from .core import GATE_EVENTS, Event
from .settings import HookHandler, Settings

_EXACT_CHARS = re.compile(r"^[A-Za-z0-9_,| \-]*$")


def matcher_matches(matcher: str, target: str) -> bool:
    """Claude Code matcher rules: '*'/'' matches all; a string of only
    letters, digits, '_', '-', spaces, ',' and '|' is exact matching with
    '|' or ',' as separators; anything else is an unanchored regex."""
    if matcher in ("", "*"):
        return True
    if _EXACT_CHARS.match(matcher):
        parts = [p.strip() for p in re.split(r"[|,]", matcher) if p.strip()]
        return target in parts
    try:
        return re.search(matcher, target) is not None
    except re.error:
        return False


def build_hook_input(event: Event, *, session_id: str | None, run_id: str,
                     cwd: str, clipboard_sha256: str = "",
                     tool_name: str = "Bash", tool_input: dict | None = None,
                     tool_use_id: str = "",
                     extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """The standard hook input object (report section 7.1)."""
    hook_input: dict[str, Any] = {
        "session_id": session_id,
        "run_id": run_id,
        "hook_event_name": event.value,
        "cwd": cwd,
        "clipboard_sha256": clipboard_sha256,
        "tool_name": tool_name,
        "tool_input": tool_input or {},
        "tool_use_id": tool_use_id or run_id,
    }
    if extra:
        hook_input.update(extra)
    return hook_input


@dataclass
class HookOutcome:
    decision: str | None = None  # allow | deny | ask (strongest seen)
    reason: str = ""
    updated_input: dict | None = None
    updated_tool_output: str | None = None  # PostRun output rewrite
    additional_context: list[str] = field(default_factory=list)
    registered_tools: list[dict] = field(default_factory=list)
    hard_block: bool = False
    hard_block_reason: str = ""
    error: str = ""  # first hook error, if any

    @property
    def is_gate(self) -> bool:
        return False  # set by dispatch() via gate_error property instead

    def gate_failed(self, event: Event) -> bool:
        """True when this outcome must block the run: a hard block, or a
        hook error on a GATE event (fail closed)."""
        if self.hard_block:
            return True
        return bool(self.error) and event in GATE_EVENTS


_DECISION_RANK = {"allow": 0, "ask": 1, "deny": 2}


def _run_handler(handler: HookHandler, hook_input: dict, timeout: float,
                 runner) -> tuple[int, str, str]:
    proc = runner(
        handler.command,
        input=json.dumps(hook_input),
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=True,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def dispatch(event: Event, hook_input: dict, settings: Settings,
             runner=subprocess.run) -> HookOutcome:
    """Run every handler whose matcher matches, in settings order.

    The match target is the input's tool_name (mutation payloads are
    reclassified to Write/Edit/Delete before this runs, exactly like
    Claude Code hooks see them).
    """
    outcome = HookOutcome()
    input_errors = validate_hook_input(hook_input)
    if input_errors:
        # The contract is enforced in code: a malformed input object
        # never reaches a handler.
        outcome.error = ("hook input schema violation: "
                         + "; ".join(input_errors))
        return outcome
    target = str(hook_input.get("tool_name", "Bash"))
    tool_input = hook_input.get("tool_input") or {}
    command_text = str(tool_input.get("command") or tool_input.get("file_path") or "")

    for handler in settings.hook_handlers(event.value):
        if not matcher_matches(handler.matcher, target):
            continue
        if handler.if_pattern and not fnmatch.fnmatch(command_text, handler.if_pattern):
            continue
        try:
            code, stdout, stderr = _run_handler(
                handler, hook_input, handler.timeout, runner)
        except subprocess.TimeoutExpired:
            outcome.error = outcome.error or (
                f"hook timed out after {handler.timeout:g}s: {handler.command}")
            continue
        except OSError as exc:
            outcome.error = outcome.error or f"hook failed to start: {exc}"
            continue

        if code == 2:
            # Hard block wins over any JSON decision, per the contract.
            outcome.hard_block = True
            reason = stderr.strip().splitlines()[0] if stderr.strip() else ""
            outcome.hard_block_reason = reason or "blocked by hook (exit 2)"
            continue
        if code != 0:
            outcome.error = outcome.error or (
                f"hook exited {code}: {handler.command}")
            continue

        stdout = stdout.strip()
        if not stdout:
            continue  # exit 0, no opinion — the common case
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            outcome.error = outcome.error or (
                f"hook printed unparseable output: {handler.command}")
            continue
        if not isinstance(parsed, dict):
            outcome.error = outcome.error or (
                f"hook output was not a JSON object: {handler.command}")
            continue

        schema_errors = validate_hook_output(parsed)
        if schema_errors:
            # A schema violation is a hook error: gate hooks fail closed
            # (stop evaluating), observational hooks fail open (the bad
            # output is ignored, the run proceeds).
            outcome.error = outcome.error or (
                f"hook output schema violation ({handler.command}): "
                + "; ".join(schema_errors))
            if event in GATE_EVENTS:
                break
            continue

        specific = parsed.get("hookSpecificOutput") or {}
        if isinstance(specific.get("updatedToolOutput"), str):
            outcome.updated_tool_output = specific["updatedToolOutput"]
        for tool in specific.get("registeredTools") or []:
            if isinstance(tool, dict):
                outcome.registered_tools.append(tool)
        decision = specific.get("permissionDecision") or parsed.get("decision")
        if isinstance(decision, str) and decision in _DECISION_RANK:
            if outcome.decision is None or (
                _DECISION_RANK[decision] > _DECISION_RANK[outcome.decision]
            ):
                outcome.decision = decision
                outcome.reason = str(
                    specific.get("permissionDecisionReason")
                    or parsed.get("reason") or "")
        if isinstance(specific.get("updatedInput"), dict):
            outcome.updated_input = specific["updatedInput"]
        context = specific.get("additionalContext") or parsed.get("additionalContext")
        if isinstance(context, str) and context:
            outcome.additional_context.append(context)

    return outcome
