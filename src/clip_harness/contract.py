"""The hook contract, enforced in code.

Why this module exists: Ali is moving off shell scripting for this
harness because shell can't hold correctness and contracts even with
Bats testing. So the Claude Code-style hook contract (§7) is not a
convention here — it is validated, in code, on both sides:

- hook INPUT (what the harness hands a handler) must match
  HOOK_INPUT_SCHEMA before dispatch;
- hook OUTPUT (what a handler prints) must match HOOK_OUTPUT_SCHEMA;
  a schema violation is a hook error, handled by the same rules as a
  crash: gate hooks fail CLOSED, observational hooks fail open.

Schemas are plain data so tests can pin them. Validation is hand-rolled
on the standard library — the core stays dependency-free.
"""

from __future__ import annotations

EVENTS = ("SessionStart", "PayloadCaptured", "PreRun", "ReviewDecision",
          "PostRun", "PostRunFailure", "RunClosed", "SessionEnd",
          "ConfigChange")

PERMISSION_DECISIONS = ("allow", "deny", "ask")

# field -> (type-name, required)
HOOK_INPUT_SCHEMA: dict[str, tuple[str, bool]] = {
    "session_id": ("str|null", True),
    "run_id": ("str", True),
    "hook_event_name": ("event", True),
    "cwd": ("str", True),
    "clipboard_sha256": ("str|null", True),
    "tool_name": ("str", True),
    "tool_input": ("object", True),
    "tool_use_id": ("str", True),
    "tool_response": ("object", False),
    "error": ("str|null", False),
    "duration_ms": ("int|null", False),
}

HOOK_OUTPUT_FIELDS: dict[str, str] = {
    "continue": "bool",
    "stopReason": "str",
    "suppressOutput": "bool",
    "systemMessage": "str",
    "hookSpecificOutput": "object",
}

HOOK_SPECIFIC_FIELDS: dict[str, str] = {
    "hookEventName": "event",
    "permissionDecision": "decision",
    "permissionDecisionReason": "str",
    "updatedInput": "object",
    "updatedToolOutput": "str",
    "additionalContext": "str",
    "registeredTools": "tool-list",
}


def _type_ok(value, kind: str) -> bool:
    if kind == "str":
        return isinstance(value, str)
    if kind == "str|null":
        return value is None or isinstance(value, str)
    if kind == "int|null":
        return value is None or (isinstance(value, int)
                                 and not isinstance(value, bool))
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "object":
        return isinstance(value, dict)
    if kind == "event":
        return isinstance(value, str) and value in EVENTS
    if kind == "decision":
        return isinstance(value, str) and value in PERMISSION_DECISIONS
    if kind == "tool-list":
        return (isinstance(value, list) and all(
            isinstance(t, dict)
            and isinstance(t.get("name"), str)
            and isinstance(t.get("description"), str)
            and (t.get("shape") is None or isinstance(t.get("shape"), str))
            for t in value))
    return False


def validate_hook_input(obj) -> list[str]:
    """Errors for a hook input object ([] = valid)."""
    if not isinstance(obj, dict):
        return ["hook input is not a JSON object"]
    errors = []
    for field, (kind, required) in HOOK_INPUT_SCHEMA.items():
        if field not in obj:
            if required:
                errors.append(f"missing required field: {field}")
        elif not _type_ok(obj[field], kind):
            errors.append(f"field {field}: expected {kind}")
    return errors


def validate_hook_output(obj) -> list[str]:
    """Errors for a hook handler's stdout object ([] = valid).

    Strict on the semantics of known fields, lenient on unknown keys
    (forward compatibility), exactly like the schema above.
    """
    if not isinstance(obj, dict):
        return ["hook output is not a JSON object"]
    errors = []
    for field, kind in HOOK_OUTPUT_FIELDS.items():
        if field in obj and not _type_ok(obj[field], kind):
            errors.append(f"field {field}: expected {kind}")
    specific = obj.get("hookSpecificOutput")
    if isinstance(specific, dict):
        for field, kind in HOOK_SPECIFIC_FIELDS.items():
            if field in specific and not _type_ok(specific[field], kind):
                errors.append(
                    f"hookSpecificOutput.{field}: expected {kind}")
        if "permissionDecision" in specific \
                and "hookEventName" not in specific:
            errors.append("hookSpecificOutput.permissionDecision "
                          "requires hookEventName")
    return errors
