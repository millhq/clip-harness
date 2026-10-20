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

MAX_CLIP_OUTPUT = 128_000  # chars; cap for the transcript's copy of output
# Default result-block budget: the OUTPUT portion of a ```result block is
# elided (head + tail digest) beyond this many chars. 100k leaves room in
# a 128k-capped chat message (Microsoft 365 Copilot's per-message limit)
# for the human's own words plus the block's command echo, footer and
# manifest. It is a default for the `result_budget_chars` SETTING — the
# limit belongs to the receiving surface, not to the protocol.
DEFAULT_RESULT_BUDGET_CHARS = 100_000
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
    """One file change proposed by a ```file, ```patch or ```edit fence.

    `declared_op` is what the payload claims (create/update/delete/auto);
    the gate classifies it against disk reality and flags mismatches.
    Exactly one of `content` (full file body), `patch` (unified diff)
    or the edit pair (`old_string` / `new_string`) is set.

    An edit spec is Claude Code's Edit tool mapped onto the fence:
    `old_string` is the exact text to find in the live file (unique,
    unless `replace_all`), `new_string` its replacement (empty =
    delete the matched block; an insert is `new_string` carrying the
    anchor plus the new text). The gate computes the candidate bytes
    from the live file — the model never hand-writes a diff — and an
    edit's declared op is always "update": edits only ever target
    existing files (creation is file.write's job).
    """

    path: str
    declared_op: str = "auto"  # create | update | delete | auto
    content: str | None = None
    patch: str | None = None
    old_string: str | None = None
    new_string: str | None = None
    replace_all: bool = False

    def __post_init__(self) -> None:
        if self.is_edit:
            self.declared_op = "update"

    @property
    def is_patch(self) -> bool:
        return self.patch is not None

    @property
    def is_edit(self) -> bool:
        return self.old_string is not None


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
    attend: bool = False  # header `attend: true` — wait inline for the
    # review decision instead of parking the run in pending_review
    writes: list[str] = field(default_factory=list)  # header `writes:`
    # — the paths a shell command DECLARES it may write (comma-separated
    # in the header, as written; resolved against the effective cwd at
    # review time). The declaration scopes the Seatbelt sandbox a
    # reviewed command runs under; it never widens anything by itself.
    transitions: list[dict] = field(default_factory=list)  # task-update
    # batch: [{"status": "done"|"doing"|"open", "title": <exact title>}]
    # parsed from an `action: task-update` payload's transition lines.
    # The batch applies atomically (all or nothing) in the goal store;
    # a task-update payload has no shell command.

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


# --- Oversized output: preserve everything, paste a digest -------------------
#
# The full output always survives byte-for-byte in the run's output file
# (the executor's spill file, runs/<run_id>/output.txt). What goes into
# the result block is a digest: past the budget, the middle is elided and
# replaced by ONE marker line that names the file and teaches the model
# how to page it (sed/grep/tail through ordinary shell commands — no new
# protocol surface). The bulk therefore never occupies the chat model's
# context, yet nothing is ever lost: the rest stays addressable.


@dataclass
class Elision:
    """The outcome of eliding one output to an allowance."""

    text: str  # the output itself, or head + marker + tail
    elided: bool
    shown_chars: int  # chars of the ORIGINAL output present in text
    total_chars: int  # len of the original output


def _elide_marker(shown: int, total: int, elided: int, output_file: str,
                  first_line: int, last_line: int) -> str:
    pointer = output_file or "the run output file"
    page_end = min(last_line, first_line + 99)
    return (
        f"[output elided: {shown} of {total} chars shown · "
        f"full output ({total} chars) saved at {pointer} · "
        f"page it with: sed -n '{first_line},{page_end}p' {pointer} · "
        f"grep {pointer} · tail -c {elided} {pointer}]"
    )


def _digest_cuts(output: str, content: int) -> tuple[str, str]:
    """Split `output` into (head, tail) totalling `content` chars.

    The shares are equal, head taking the odd char. Cuts prefer line
    boundaries: the head ends at the last newline in its share when that
    wastes at most half the share (no partial line is shown), and the
    tail starts just after the first newline in its share's first half.
    With no usable boundary the cut is a hard char cut.
    """
    total = len(output)
    head_share = (content + 1) // 2
    tail_share = content // 2
    head_end = head_share
    newline = output.rfind("\n", 0, head_share)
    if newline >= head_share // 2:
        head_end = newline  # the newline itself is not shown either
    tail_start = total - tail_share
    newline = output.find("\n", tail_start, tail_start + tail_share // 2)
    if newline != -1:
        tail_start = newline + 1
    if head_end >= tail_start:  # cannot happen (shares sum < total)
        head_end, tail_start = head_share, total - tail_share
    return output[:head_end], output[tail_start:]


def _line_range(output: str, head: str, tail: str) -> tuple[int, int]:
    """1-based (first, last) line numbers touched by the elided middle —
    the sed paging hint in the marker. Exact at line-boundary cuts."""
    first = output.count("\n", 0, len(head)) + 1
    last = output.count("\n", 0, len(output) - len(tail)) + 1
    last = min(last, output.count("\n") + 1)
    return first, max(first, last)


def elide_output(output: str, allowance: int,
                 output_file: str = "") -> Elision:
    """Elide `output` to at most `allowance` chars (see the section
    header above). Output within the allowance is returned untouched.

    The marker's length depends on the shown count, which depends on
    the space the marker leaves — so the marker's width is reserved at
    its worst case (every number as wide as the total) before cutting;
    the real marker is never wider, and the assembled digest never
    exceeds the allowance. Char counts in the marker are exact:
    shown = head chars + tail chars, total = the full output length.
    """
    total = len(output)
    if total <= allowance:
        return Elision(output, False, total, total)
    worst = _elide_marker(total, total, total, output_file, total, total)
    content = allowance - len(worst) - 2  # the two "\n" around the marker
    if content < 0:
        # Pathological budget: the marker alone busts the allowance.
        # The marker IS the digest — it stays whole and exact.
        marker = _elide_marker(0, total, total, output_file, 1,
                               output.count("\n") + 1)
        return Elision(marker, True, 0, total)
    head, tail = _digest_cuts(output, content)

    def assemble(head: str, tail: str) -> tuple[str, str]:
        shown = len(head) + len(tail)
        first, last = _line_range(output, head, tail)
        marker = _elide_marker(shown, total, total - shown, output_file,
                               first, last)
        if not head and not tail:
            return marker, marker
        body = head + "\n" + marker + ("\n" + tail if tail else "")
        return body, marker

    text, marker = assemble(head, tail)
    if len(text) > allowance:
        # Freak digit-width case (a line count wider than the char
        # total it was reserved against): shave the excess off the
        # head's end, then the tail's start, and rebuild once — a
        # smaller shown count never widens the marker.
        excess = len(text) - allowance
        cut = min(excess, len(head))
        head, excess = head[:len(head) - cut], excess - cut
        if excess:
            tail = tail[excess:]
        text, marker = assemble(head, tail)
    return Elision(text, True, len(head) + len(tail), total)


# --- Result formatting (format inherited from clipboard_loop.py) --------------


def format_result(command: str, output: str, exit_desc: str, elapsed: float,
                  output_file: str = "", *, files_section: str = "",
                  standing_section: str = "",
                  extra_section: str = "",
                  command_note: str = "",
                  budget: int | None = None) -> str:
    """The ```result block that goes back on the clipboard.

    The output portion is bounded by `budget` chars (the
    `result_budget_chars` setting; DEFAULT_RESULT_BUDGET_CHARS when not
    passed): beyond it the output is elided to a head + tail digest
    around the elision marker, and the full output stays in
    `output_file`. The command echo and the sections below are not part
    of the budget — they stay whole.

    Optional sections, appended inside the fence after the exit line:
    `files_section` (the applied/rejected file outcome), then
    `extra_section` (hook context, a refreshed capability manifest,
    pending state — the variable middle), then `standing_section` LAST:
    the sticky footer (the core-generated [goal] / [open tasks] lines,
    bound runs only). The footer has a fixed shape and a fixed
    position — it is always the final section of a bound block.

    `command_note` is the ONE annotation a reviewed shell run adds:
    a parenthetical on the command echo line (e.g. "reviewed ·
    sandboxed") recording that the command was held for review and
    the mode the human approved. Empty for ordinary runs — the block
    is otherwise byte-stable.
    """
    body = output if output else "(no output)"
    if output:
        limit = DEFAULT_RESULT_BUDGET_CHARS if budget is None else budget
        body = elide_output(output, limit, output_file).text
    echo = f"$ {command}" + (f" ({command_note})" if command_note else "")
    block = f"```result\n{echo}\n{body}\n[exit {exit_desc} · {elapsed:.1f}s]"
    for section in (files_section, extra_section, standing_section):
        if section:
            block += f"\n{section}"
    return block + "\n```"


def cap_output(output: str, output_file: str = "") -> tuple[str, bool]:
    """Return (possibly-elided output, was_elided) for the transcript."""
    result = elide_output(output, MAX_CLIP_OUTPUT, output_file)
    return result.text, result.elided
