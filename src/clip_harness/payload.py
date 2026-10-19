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
        header; recognised keys are session, action, cwd, timeout. The
        rest of the fence is shell text. `session:` binds the run to that
        session id (addressable for cancel/rerun, receipt keyed by it);
        without it the run is unbound but still runs.

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
    r"^(session|action|cwd|timeout)\s*:\s*(.*?)\s*$", re.IGNORECASE)
_PATH_LINE = re.compile(r"^path\s*:\s*(.+?)\s*$", re.IGNORECASE)
_OP_LINE = re.compile(r"^op\s*:\s*(create|update|delete|auto)\s*$", re.IGNORECASE)
_ATTR_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)=(.*)$")
_ID_KEYS = {"id", "session"}


class PayloadError(ValueError):
    """The clipboard looks like a payload but is malformed."""


def looks_like_trigger(text: str) -> bool:
    """True when the text attempts to be a payload (even a malformed one)."""
    stripped = text.strip()
    if any(stripped.startswith("```" + w) for w in ("cancel", "rerun")):
        return True
    return _FENCE_OPEN_COUNT.search(text) is not None


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
    return payload


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
    if not payload.command.strip() and not files and payload.kind == "run":
        raise PayloadError("run payload has an empty command")
    return payload


# Markers of the harness's OWN clipboard output — never re-run these as
# bare commands if the user hits the trigger while one is on the clipboard.
_OWN_OUTPUT_PREFIXES = ("```result", "⏳", "⛔", "✗ clip-harness")


def parse_clipboard_text(text: str, *, allow_bare: bool = False) -> Payload | None:
    """The payload detection ladder.

        1. ```shell fence (metadata header + commands)
        2. ```file / ```patch fences (mutation payloads)
        3. bare clipboard text -> a single unbound command

    The fence/metadata layer is progressive enhancement over "whatever
    is on the clipboard runs" — never a requirement for the simple
    case (Ali's hotkey-run tool already worked this way). Bare text is
    only accepted when `allow_bare` is set: the explicit trigger path
    (the human hit the hotkey and ran the CLI) passes True; the passive
    watch loop passes False so arbitrary clipboard content never
    auto-executes.
    """
    payload = parse_payload(text)
    if payload is not None or not allow_bare:
        return payload
    stripped = text.strip()
    if not stripped:
        return None
    if stripped.startswith(_OWN_OUTPUT_PREFIXES):
        return None
    return Payload(command=stripped, kind="run", raw=text)
