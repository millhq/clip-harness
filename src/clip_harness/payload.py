"""Clipboard payload parsing.

THE PROTOCOL (v1 — the block format is still being co-designed with its
user; this module is the one place that knows it):

A copied message is ONE payload made of fences:

    ```shell
    session: auth-refactor
    action: run the tests
    cwd: ~/work/api
    timeout: 300
    npm test
    ```
        Commands to run. The leading `key: value` lines are the metadata
        header; recognised keys are session, action, cwd, timeout,
        attend, writes. The
        rest of the fence is shell text. `session:` binds the run to that
        session id (addressable for cancel/rerun, receipt keyed by it);
        without it the run is unbound but still runs. `writes:` is a
        comma-separated declaration of the paths the command may write
        — it scopes the sandbox when the command is held for review;
        an empty value is malformed (fail closed).

        Goal/task action payloads (`action: goal-set` / `goal-status` /
        `task-add` / `task-update`) carry no shell text: the body is
        action data. A `task-update` body is one
        `<status>: <exact task title>` transition per line (status
        done / doing / open, repeatable), parsed here into
        `payload.transitions` — the batch the goal store validates
        and applies atomically, or not at all. Malformed transition
        lines fail closed, like malformed header metadata.

    ```file
    path: src/auth.py
    op: update
    <full file content>
    ```
        A proposed file write. `op:` is optional (create/update/delete);
        without it the gate classifies against disk. Ali reviews files
        on disk in VS Code, never in the chat — so file fences always
        carry the FULL content, never a description of it.

    ```patch
    path: src/auth.py
    <unified diff>
    ```
        A proposed change as a unified diff against the live file.

File fences with no shell fence = a pure mutation payload. The canonical
command fence is ```shell; ```run is accepted as a legacy alias only.

THE UNFENCED FORM (a copy-tolerance, not a second protocol): some chat
UIs — Microsoft 365 Copilot is the known one — copy a code block's
CONTENTS without the outer fence when the human uses the block's copy
button. The model still emits exactly one canonical form (the fence);
the harness additionally recognises the fence's contents standing
alone: text that BEGINS with a metadata header (recognised `key:` lines,
contiguous from line 1) parses as the shell payload that header
describes, and the header lines never reach the shell. Text that does
not begin with a recognised metadata line is not an unfenced payload.
Malformed metadata (a recognised key with an invalid value) fails
closed — rejected, never executed as bare text.

The same tolerance covers the mutation fences: text whose first line
is an exact `path:` header parses as the file fence's contents when
the next line is an explicit `op:` line (file.write REQUIRES an op in
this form, and stricter than the fence: create/update/delete only —
no auto — with a non-empty body for create/update and an empty body
for delete), or as the patch fence's contents when the body begins
with a unified-diff header (`---` / `+++`). An invalid `op:` value,
or a `path:` header followed by neither, fails closed — such text is
never treated as bare shell text and never staged.

Control triggers stay single-line, as in the original loop:
    ```cancel```  /  ```cancel id=<session>```
    ```rerun```    /  ```rerun id=<session>```
"""

from __future__ import annotations

import re

from .core import FileSpec, Payload

_FENCE_OPEN = re.compile(
    r"^```(shell|run|file|patch)([^\n]*)\n(.*?)^```[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
_FENCE_OPEN_COUNT = re.compile(
    r"^```(shell|run|file|patch)\b", re.MULTILINE)
_HEADER_LINE = re.compile(
    r"^(session|action|cwd|timeout|attend|writes)\s*:\s*(.*?)\s*$",
    re.IGNORECASE)
_PATH_LINE = re.compile(r"^path\s*:\s*(.+?)\s*$", re.IGNORECASE)
_OP_LINE = re.compile(r"^op\s*:\s*(create|update|delete|auto)\s*$", re.IGNORECASE)
_OP_ATTEMPT = re.compile(r"^op\s*:", re.IGNORECASE)
# The unfenced file form accepts only the explicit ops — `auto` is
# fenced-only, where the fence itself marks the text as a payload.
_OP_LINE_UNFENCED = re.compile(
    r"^op\s*:\s*(create|update|delete)\s*$", re.IGNORECASE)
_ATTR_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)=(.*)$")
_ID_KEYS = {"id", "session"}
_TRANSITION_LINE = re.compile(
    r"^(done|doing|open)\s*:\s*(.+?)\s*$", re.IGNORECASE)
_TRANSITION_KEY = re.compile(r"^([A-Za-z][A-Za-z-]*)\s*:\s*(.*?)\s*$")
_TASK_STATUSES = ("done", "doing", "open")


def _parse_task_transitions(body: str) -> list[dict]:
    """Parse the body of an `action: task-update` payload.

    One transition per line: `<status>: <exact task title>` with
    status done / doing / open, repeatable (multiple titles per
    status via multiple lines). The batch is atomic — set-level
    validation (unknown / duplicate / conflicting titles) happens
    in the goal store; here, any line that is not a well-formed
    transition fails closed, exactly like malformed header metadata:
    an unknown status key, a missing title, or a line with no
    status at all is a PayloadError, never shell text.
    """
    transitions: list[dict] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _TRANSITION_LINE.match(line)
        if match:
            transitions.append({"status": match.group(1).lower(),
                                "title": match.group(2)})
            continue
        keyed = _TRANSITION_KEY.match(line)
        if keyed and keyed.group(1).lower() in _TASK_STATUSES:
            raise PayloadError(
                f"task-update line {line!r} is missing a task title "
                f"— expected '<status>: <exact task title>'")
        if keyed:
            raise PayloadError(
                f"task-update line {line!r} has an unknown status "
                f"{keyed.group(1)!r} — expected done, doing or open")
        raise PayloadError(
            f"malformed task-update line {line!r} — expected "
            f"'<status>: <exact task title>' with status done, doing "
            f"or open")
    if not transitions:
        raise PayloadError(
            "task-update payload has no transitions — one "
            "'<status>: <exact task title>' line per change")
    return transitions


class PayloadError(ValueError):
    """The clipboard looks like a payload but is malformed."""


def looks_like_trigger(text: str) -> bool:
    """True when the text attempts to be a payload (even a malformed one)."""
    stripped = text.strip()
    if any(stripped.startswith("```" + w) for w in ("cancel", "rerun")):
        return True
    if _FENCE_OPEN_COUNT.search(text) is not None:
        return True
    # Unfenced form: a leading recognised metadata line, or an exact
    # `path:` header (the mutation fences' contents), is a payload
    # attempt (validated at parse time, fail-closed).
    first_line = text.split("\n", 1)[0]
    return (_HEADER_LINE.match(first_line) is not None
            or _PATH_LINE.match(first_line) is not None)


def _parse_attrs(text: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for token in text.split():
        match = _ATTR_RE.match(token)
        if match:
            key, value = match.group(1).lower(), match.group(2).strip("\"'")
            if key in _ID_KEYS:
                attrs["id"] = value
            else:
                attrs[key] = value
        else:
            attrs.setdefault("id", token.strip("\"'"))
    return attrs


def _parse_control(text: str) -> Payload | None:
    """Single-line control triggers: ```cancel```, ```rerun id=x```, ..."""
    stripped = text.strip()
    if "\n" in stripped or not stripped.endswith("```"):
        return None
    for kind in ("cancel", "rerun"):
        prefix = "```" + kind
        if not stripped.startswith(prefix):
            continue
        rest = stripped[len(prefix):-3]
        if rest and rest[0] not in (" ", "\t"):
            return None  # ```cancelled is not a trigger
        attrs = _parse_attrs(rest.strip())
        return Payload(command="", session_id=attrs.get("id") or None,
                       raw=text, kind=kind)
    return None


def _parse_shell_body(body: str, fence_attrs: dict[str, str]) -> Payload:
    """Split a shell fence body into metadata header + command text."""
    lines = body.split("\n")
    header: dict[str, str] = {}
    idx = 0
    while idx < len(lines):
        match = _HEADER_LINE.match(lines[idx])
        if not match:
            break
        header[match.group(1).lower()] = match.group(2)
        idx += 1
    command = "\n".join(lines[idx:]).strip()
    payload = Payload(command=command, kind="run")
    session = header.get("session") or fence_attrs.get("id") or ""
    payload.session_id = session.strip() or None
    payload.action = header.get("action", "")
    payload.cwd = header.get("cwd", "")
    if header.get("timeout"):
        try:
            payload.timeout = float(header["timeout"])
        except ValueError:
            raise PayloadError(
                f"bad timeout in payload header: {header['timeout']!r}")
    if header.get("attend"):
        payload.attend = header["attend"].strip().lower() in (
            "true", "yes", "1")
    if "writes" in header:
        # `writes:` declares the paths the command may write — the
        # scope a reviewed (sandboxed) run is confined to. An empty
        # value is malformed metadata, not "no declaration": fail
        # closed, exactly like a bad timeout.
        entries = [part.strip() for part in header["writes"].split(",")]
        entries = [entry for entry in entries if entry]
        if not entries:
            raise PayloadError(
                "empty 'writes:' in payload header — declare the "
                "paths the command may write, comma-separated, or "
                "omit the header")
        payload.writes = entries
    if payload.action == "task-update":
        # The body of a task-update payload is not shell text: it is
        # the transition batch (see _parse_task_transitions). Parse
        # and consume it here, in the same place the header grammar
        # lives, so a transition line can never reach the shell —
        # the same guarantee the metadata header has.
        payload.transitions = _parse_task_transitions(command)
        payload.command = ""
    return payload


# Values the `attend:` header accepts in the unfenced form. The fenced
# parser maps anything outside ("true", "yes", "1") to False; the
# unfenced form validates strictly instead, because there is no fence
# to mark the text as a deliberate payload — a recognised key carrying
# a nonsense value is malformed metadata, not a bare command.
_ATTEND_VALUES = ("true", "false", "yes", "no", "1", "0")


def _parse_unfenced(text: str) -> Payload | None:
    """Parse the fence-stripped form of a shell payload.

    Some chat UIs copy a code block's contents without its ``` fence
    (Microsoft 365 Copilot's copy button is the known case). When the
    text BEGINS with metadata header lines, it is the shell fence's
    contents standing alone:

        session: auth-refactor
        timeout: 30
        npm test

    The header is the maximal contiguous run of leading lines that
    match a recognised metadata key (session / action / cwd /
    timeout / attend / writes) — the same `_HEADER_LINE` grammar and the same
    value handling as `_parse_shell_body`, which does the actual
    parsing. The first line that is not a recognised-key line ends
    the header; everything from there on is the body, even lines that
    look like metadata. Recognised header lines never reach the
    shell.

    Returns None when the text does not begin with a metadata line
    (it is then bare text, handled by the caller's bare rung).
    Raises PayloadError — fail closed, nothing executes — when a
    header line carries an invalid value, or when the payload is
    metadata with no command body.
    """
    lines = text.split("\n")
    idx = 0
    while idx < len(lines):
        match = _HEADER_LINE.match(lines[idx])
        if not match:
            break
        key, value = match.group(1).lower(), match.group(2)
        if key == "attend" and value \
                and value.lower() not in _ATTEND_VALUES:
            raise PayloadError(
                f"bad attend in payload header: {value!r} "
                f"(expected true or false)")
        idx += 1
    if idx == 0:
        return None
    # Same grammar, same assignments, same timeout validation: the
    # fenced body parser cannot tell this text was never fenced.
    payload = _parse_shell_body(text, {})
    payload.raw = text
    if not payload.command.strip() and not payload.transitions:
        raise PayloadError(
            "unfenced payload has a metadata header but no command")
    return payload


def _parse_unfenced_file(text: str) -> Payload | None:
    """Parse the fence-stripped form of a file / patch payload.

    The mutation fences' contents standing alone (what a fence-
    stripping copy button leaves on the clipboard):

        path: src/auth.py
        op: update
        <full file content>

        path: src/tokens.py
        --- a/src/tokens.py
        +++ b/src/tokens.py
        <unified diff>

    Recognition is deliberately stricter than the fenced grammar, so
    ordinary text that merely begins with `path:` cannot be
    misclassified:

    - line 1 must be an exact `path:` header (the same `_PATH_LINE`
      grammar the fences use);
    - a following `op:` line makes it a FILE payload, and file.write
      REQUIRES that explicit op in this form. The op must be
      create, update or delete — `auto` is fenced-only here, and
      any other value is malformed. create/update REQUIRE a
      non-empty body (the file content; there is no explicit
      empty-file support), and delete REQUIRES an empty body;
    - with no `op:` line, the body must begin with a unified-diff
      header (first line starts `---`, second starts `+++`) to be a
      PATCH payload. The op wins when both are present: a file whose
      content is itself a diff is legitimate.

    Returns None when line 1 is not a `path:` header. Once line 1
    matches, every failure raises PayloadError — fail closed: the
    text is never treated as bare shell text and never staged. The
    FileSpec itself is built by `_parse_file_body`, so the runner's
    staging/review/apply path cannot tell this payload was unfenced.
    """
    lines = text.split("\n")
    if not lines or not _PATH_LINE.match(lines[0]):
        return None
    rest = lines[1:]
    if rest and _OP_ATTEMPT.match(rest[0]):
        op_match = _OP_LINE_UNFENCED.match(rest[0])
        if not op_match:
            raise PayloadError(
                f"unfenced file payload has an invalid 'op:' line: "
                f"{rest[0].strip()!r} (expected create, update or "
                f"delete — auto is fenced-only)")
        op = op_match.group(1).lower()
        body = "\n".join(rest[1:])
        if op == "delete" and body.strip():
            raise PayloadError(
                "unfenced 'op: delete' payload must have an empty "
                "body")
        if op in ("create", "update") and not body.strip():
            raise PayloadError(
                f"unfenced 'op: {op}' payload needs the file "
                f"content as its body")
        spec = _parse_file_body("file", text)
        return Payload(command="", kind="run", files=[spec], raw=text)
    if len(rest) >= 2 and rest[0].startswith("---") \
            and rest[1].startswith("+++"):
        spec = _parse_file_body("patch", text)
        return Payload(command="", kind="run", files=[spec], raw=text)
    raise PayloadError(
        "unfenced file/patch payload needs an 'op:' line (file) or "
        "a unified diff beginning '---' / '+++' (patch) after the "
        "'path:' line")


def _parse_file_body(kind: str, body: str) -> FileSpec:
    lines = body.split("\n")
    # Tolerate leading blank lines before the path: header.
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start >= len(lines):
        raise PayloadError(f"{kind} fence is empty")
    match = _PATH_LINE.match(lines[start])
    if not match:
        raise PayloadError(f"{kind} fence must start with a 'path:' line")
    path = match.group(1)
    idx = start + 1
    op = "auto"
    if idx < len(lines):
        op_match = _OP_LINE.match(lines[idx])
        if op_match:
            op = op_match.group(1).lower()
            idx += 1
    rest = "\n".join(lines[idx:])
    if op == "delete":
        return FileSpec(path=path, declared_op="delete", content="")
    if kind == "patch":
        patch = rest.strip("\n")
        if not patch:
            raise PayloadError(f"patch fence for {path} has an empty diff")
        return FileSpec(path=path, declared_op=op, patch=patch)
    # Full content: keep verbatim except one leading newline after headers.
    content = rest[1:] if rest.startswith("\n") else rest
    return FileSpec(path=path, declared_op=op, content=content)


def parse_payload(text: str) -> Payload | None:
    """Parse clipboard text into a Payload.

    Returns None when the text carries no payload. Raises PayloadError
    when it looks like a payload but is malformed, so the caller can
    receipt the rejection instead of silently ignoring it.
    """
    control = _parse_control(text)
    if control is not None:
        return control

    matches = list(_FENCE_OPEN.finditer(text))
    openings = len(_FENCE_OPEN_COUNT.findall(text))
    if openings > len(matches):
        raise PayloadError("payload has an unclosed fence")
    if not matches:
        return None

    payload: Payload | None = None
    files: list[FileSpec] = []
    for match in matches:
        kind, attr_text, body = match.group(1), match.group(2), match.group(3)
        if kind in ("shell", "run"):
            if payload is not None:
                raise PayloadError("payload has more than one shell fence")
            payload = _parse_shell_body(body, _parse_attrs(attr_text))
        else:
            files.append(_parse_file_body(kind, body))

    if payload is None:
        payload = Payload(command="", kind="run")  # pure mutation payload
    payload.files = files
    payload.raw = text
    if not payload.command.strip() and not files \
            and not payload.transitions and payload.kind == "run":
        raise PayloadError("run payload has an empty command")
    return payload


# Markers of the harness's OWN clipboard output — never re-run these as
# bare commands if the user hits the trigger while one is on the clipboard.
_OWN_OUTPUT_PREFIXES = ("```result", "⏳", "⛔", "✗ clip-harness")


def parse_clipboard_text(text: str, *, allow_bare: bool = False) -> Payload | None:
    """The payload detection ladder.

        1. ```shell fence (metadata header + commands)
        2. ```file / ```patch fences (mutation payloads)
        3. unfenced text beginning with a metadata header — the
           shell fence's contents as copied by UIs that strip the
           fence (Microsoft 365 Copilot's copy button)
        4. unfenced text beginning with a `path:` header — the file
           / patch fences' contents, under stricter rules (an
           explicit `op:` line for a file, a diff header for a
           patch; anything else fails closed)
        5. bare clipboard text -> a single unbound command

    The fence/metadata layer is progressive enhancement over "whatever
    is on the clipboard runs" — never a requirement for the simple
    case (Ali's hotkey-run tool already worked this way). Rungs 1–4
    are payload forms: a fence or a protocol header is a deliberate
    protocol act, accepted wherever payloads are. Bare text is only
    accepted when `allow_bare` is set: the explicit trigger path
    (the human hit the hotkey and ran the CLI) passes True; the passive
    watch loop passes False so arbitrary clipboard content never
    auto-executes.
    """
    payload = parse_payload(text)
    if payload is not None:
        return payload
    payload = _parse_unfenced(text)
    if payload is None:
        payload = _parse_unfenced_file(text)
    if payload is not None or not allow_bare:
        return payload
    stripped = text.strip()
    if not stripped:
        return None
    if stripped.startswith(_OWN_OUTPUT_PREFIXES):
        return None
    return Payload(command=stripped, kind="run", raw=text)
