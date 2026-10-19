"""Core data types, constants and result formatting for clip-harness.

Everything in this module is platform-independent: no macOS, no iTerm2,
no clipboard access. The Mac-specific pieces live in clip_harness.adapters
behind the protocols in clip_harness.adapters.base.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

# --- Limits (carried over from the original clipboard_loop.py) ---------------

MAX_CLIP_OUTPUT = 128_000  # chars; clipboard results are capped at 128k
MAX_PAYLOAD_CHARS = 32_768  # refuse absurdly long payloads (hotkey-run guard)
DEFAULT_TIMEOUT = 120  # per-command timeout, seconds
DEFAULT_HOOK_TIMEOUT = 30  # per-hook timeout, seconds
DEFAULT_GATE_TIMEOUT = 10  # gate hooks are kept short on purpose


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def new_run_id() -> str:
    return "r-" + uuid.uuid4().hex[:8]


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


# --- Lifecycle events ---------------------------------------------------------
#
# Harness analogues of the Claude Code hook lifecycle, per the applied
# mapping in the hooks-lifecycle research report (sections 6-8).


class Event(str, Enum):
    SESSION_START = "SessionStart"  # harness armed
    PAYLOAD_CAPTURED = "PayloadCaptured"  # UserPromptSubmit analogue
    PRE_RUN = "PreRun"  # PreToolUse analogue — the gate
    REVIEW_DECISION = "ReviewDecision"  # PermissionRequest analogue
    POST_RUN = "PostRun"  # PostToolUse analogue
    POST_RUN_FAILURE = "PostRunFailure"  # PostToolUseFailure analogue
    RUN_CLOSED = "RunClosed"  # Stop analogue
    SESSION_END = "SessionEnd"
    CONFIG_CHANGE = "ConfigChange"


# Gate events FAIL CLOSED: a hook error (crash, timeout, unparseable output)
# blocks the run. Observational events fail open.
GATE_EVENTS = frozenset(
    {
        Event.PAYLOAD_CAPTURED,
        Event.PRE_RUN,
        Event.REVIEW_DECISION,
        Event.RUN_CLOSED,
    }
)
OBSERVATIONAL_EVENTS = frozenset(set(Event) - GATE_EVENTS)


class RunState(str, Enum):
    CAPTURED = "captured"
    GATED = "gated"
    PENDING_REVIEW = "pending_review"  # staged candidate, no decision yet
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"  # gate deny / hard block — never executed
    REJECTED = "rejected"  # human rejected the candidate at review
    CANCELLED = "cancelled"
    CLOSED = "closed"


class Decision(str, Enum):
    """A gate decision, mirroring Claude Code's permissionDecision values."""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass
class FileSpec:
    """One file change proposed by a ```file or ```patch fence.

    `declared_op` is what the payload claims (create/update/delete/auto);
    the gate classifies it against disk reality and flags mismatches.
    Exactly one of `content` (full file body) or `patch` (unified diff)
    is set.
    """

    path: str
    declared_op: str = "auto"  # create | update | delete | auto
    content: str | None = None
    patch: str | None = None

    @property
    def is_patch(self) -> bool:
        return self.patch is not None


@dataclass
class Payload:
    """A parsed clipboard payload: one optional ```shell fence plus any
    number of ```file / ```patch fences.

    `session_id` comes from the block's metadata header: when present the
    run is *bound* to that session (addressable for cancel/rerun, receipt
    keyed by it); when absent the run is unbound but still executes — a
    missing id never blocks a run. A payload with file fences but no shell
    commands is a pure mutation payload.
    """

    command: str = ""
    session_id: str | None = None
    raw: str = ""
    kind: str = "run"  # run | cancel | rerun
    files: list[FileSpec] = field(default_factory=list)
    action: str = ""  # free-form header label, echoed into the receipt
    cwd: str = ""  # header cwd override ("" = inherit)
    timeout: float | None = None  # header timeout override, seconds

    @property
    def bound(self) -> bool:
        return self.session_id is not None

    @property
    def pure_mutation(self) -> bool:
        return bool(self.files) and not self.command.strip()


@dataclass
class GateVerdict:
    """The combined outcome of built-in policy + PreRun hooks."""

    decision: Decision = Decision.ALLOW
    reason: str = ""
    hard_block: bool = False  # exit-2 semantics: blocked before allowlist
    updated_command: str | None = None  # updatedInput rewrite, if any
    hook_error: str = ""  # set when a gate hook failed (fail-closed)

    @property
    def blocked(self) -> bool:
        return self.decision == Decision.DENY or bool(self.hook_error)


@dataclass
class ExecResult:
    output: str = ""
    exit_desc: str = "0"  # exit code as text, or "cancelled" / "timeout"
    exit_code: int | None = 0
    elapsed: float = 0.0
    timed_out: bool = False
    cancelled: bool = False
    output_file: str = ""

    @property
    def failed(self) -> bool:
        return self.exit_desc not in ("0",)


@dataclass
class RunContext:
    """Everything one run carries through the lifecycle."""

    run_id: str
    payload: Payload
    cwd: str
    session_id: str | None = None  # the day/session grouping id
    clipboard_sha256: str = ""
    state: RunState = RunState.CAPTURED
    command_executed: str = ""  # post-rewrite command that actually ran
    candidate_id: str | None = None
    result: ExecResult | None = None
    started_at: float = field(default_factory=time.time)
    extra: dict = field(default_factory=dict)

    @property
    def binding_id(self) -> str | None:
        """The session id this run is bound to, if the payload carried one."""
        return self.payload.session_id


# --- Result formatting (format inherited from clipboard_loop.py) --------------


def format_result(command: str, output: str, exit_desc: str, elapsed: float,
                  output_file: str = "", *, files_section: str = "",
                  standing_section: str = "",
                  extra_section: str = "") -> str:
    """The ```result block that goes back on the clipboard.

    Output is capped at MAX_CLIP_OUTPUT chars; beyond that the block is
    truncated with a pointer to the file holding the full output.

    Optional sections, appended inside the fence after the exit line:
    `files_section` (the applied/rejected file outcome), then
    `standing_section` (the core goal/task nag — bound runs only), then
    `extra_section` (e.g. a refreshed capability manifest).
    """
    body = output if output else "(no output)"
    if len(body) > MAX_CLIP_OUTPUT:
        pointer = output_file or "the run output file"
        body = (
            body[:MAX_CLIP_OUTPUT]
            + f"\n…[truncated at 128k chars, full output in {pointer}]"
        )
    block = f"```result\n$ {command}\n{body}\n[exit {exit_desc} · {elapsed:.1f}s]"
    for section in (files_section, standing_section, extra_section):
        if section:
            block += f"\n{section}"
    return block + "\n```"


def cap_output(output: str, output_file: str = "") -> tuple[str, bool]:
    """Return (possibly-truncated output, was_truncated) for receipts."""
    if len(output) <= MAX_CLIP_OUTPUT:
        return output, False
    pointer = output_file or "the run output file"
    return (
        output[:MAX_CLIP_OUTPUT]
        + f"\n…[truncated at 128k chars, full output in {pointer}]"
    ), True
