"""Seatbelt sandbox profiles for reviewed shell commands.

When a held shell command is accepted SANDBOXED at review, the
executor wraps it in `sandbox-exec -p <profile>` (macOS Seatbelt).
This module owns the profile: a PURE function of (effective cwd,
declared `writes:` paths, temp dirs, config dirs) rendered as text,
so it is unit-testable anywhere — including this Linux box, where
sandbox-exec does not exist and a sandboxed accept must fail closed
in the executor instead of ever running bare.

Profile posture (mirroring Claude Code's sandbox defaults):

- reads are broadly allowed — `(allow default)` permits everything
  not explicitly denied, and the only denials are file WRITES;
- file writes are denied by default, then allowed for subtrees:
  the effective cwd, each declared `writes:` path, and the temp
  dirs — plus the `/dev/null` literal, which shells expect to be
  able to open for writing;
- hard write-denies that NO allow overrides sit INSIDE otherwise
  writable roots and are listed LAST (Seatbelt rules are evaluated
  in order, the last matching rule wins): `<cwd>/.git/hooks` (a
  writable hook is code execution on the next git command),
  `<cwd>/.git/config` (same class), the user's clip-harness config
  dir (a command must not rewrite the gate's own settings/hooks),
  and `<cwd>/.clip-harness` (project settings — an always-allow
  pattern must come from the human, never from the command);
- process exec/fork is allowed (the default posture), and network
  is left unrestricted in this slice — a stated non-goal, not an
  oversight: no network rules are emitted at all.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from .settings import USER_SETTINGS


def sandbox_exec_available() -> bool:
    """True when `sandbox-exec` is on PATH (macOS ships it; other
    platforms fail closed on sandboxed accepts)."""
    return shutil.which("sandbox-exec") is not None


def user_config_dir() -> Path:
    """The user's clip-harness config dir (~/.config/clip-harness) —
    a hard write-deny in every profile."""
    return USER_SETTINGS.expanduser().parent


def default_tmpdirs() -> list[Path]:
    """The temp dirs a sandboxed command may write: $TMPDIR, the
    interpreter's temp dir, and /tmp — resolved and de-duplicated."""
    found: list[Path] = []
    for raw in (os.environ.get("TMPDIR"), tempfile.gettempdir(), "/tmp"):
        if not raw:
            continue
        path = Path(raw).expanduser().resolve()
        if path not in found:
            found.append(path)
    return found


def protected_carveouts(cwd: Path,
                        config_dir: Path | None = None
                        ) -> list[tuple[Path, bool]]:
    """(path, is_file) pairs that stay write-denied even inside a
    writable root, in profile order. Files are denied with a
    `literal` rule, subtrees with a `subpath` rule."""
    config_dir = (config_dir if config_dir is not None
                  else user_config_dir())
    return [(cwd / ".git" / "hooks", False),
            (cwd / ".git" / "config", True),
            (Path(config_dir).expanduser().resolve(), False),
            (cwd / ".clip-harness", False)]


def _sbpl_string(path: Path) -> str:
    """One path as a quoted Seatbelt (Scheme-like) string literal.
    Backslashes and double quotes are escaped — the only two escapes
    the profile syntax honours inside a string."""
    text = str(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def build_profile(*, cwd: str | Path, writes: list[str | Path] = (),
                  tmpdirs: list[Path] | None = None,
                  config_dir: Path | None = None) -> str:
    """The Seatbelt profile text for one sandboxed execution.

    `writes` are the command's declared write paths, already resolved
    to absolute paths by the caller (gate.resolve_writes). Pure:
    same inputs, same text.
    """
    cwd = Path(cwd).expanduser().resolve()
    allowed: list[Path] = [cwd]
    for entry in writes:
        path = Path(entry).expanduser().resolve()
        if path not in allowed:
            allowed.append(path)
    for path in (tmpdirs if tmpdirs is not None else default_tmpdirs()):
        if path not in allowed:
            allowed.append(path)

    lines = [
        "(version 1)",
        "; clip-harness reviewed-shell sandbox — reads broad, "
        "writes confined to the declared scope; protected paths "
        "stay denied (last matching rule wins).",
        "(allow default)",
        "(deny file-write*)",
        "(allow file-write*",
        '  (literal "/dev/null")',
    ]
    lines += [f"  (subpath {_sbpl_string(path)})" for path in allowed]
    lines.append(")")
    lines.append("(deny file-write*")
    for path, is_file in protected_carveouts(cwd, config_dir):
        rule = "literal" if is_file else "subpath"
        lines.append(f"  ({rule} {_sbpl_string(path)})")
    lines.append(")")
    return "\n".join(lines) + "\n"
