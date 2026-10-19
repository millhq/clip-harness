"""Terminal rendering: color for diffs, applied ONLY at print time.

Color lives in the rendering layer, never in stored or sent text.
Result blocks, clipboard content, receipts, and rejection text stay
byte-for-byte plain; when (and only when) a block or presentation is
printed to a terminal, the print site paints a COPY of it through
:func:`paint`. The string that goes to the clipboard is always the
original.

Diff coloring follows the usual convention: additions green,
deletions red, ``---``/``+++`` file headers bold cyan, ``@@`` hunk
headers cyan, context lines left in the default color. Detection is
region-based: a diff region starts at a ``--- ``/``+++ `` header pair
and runs while lines keep the headers' indentation and a diff-line
shape — so look-alike lines elsewhere (``ls -la`` output, bullet
lists) are never painted, and non-diff text passes through untouched.

Gating (see :func:`should_color`): the ``color`` settings key —
``"auto"`` (default), ``"always"``, ``"never"``. ``auto`` colors
only when stdout is a TTY; ``always`` colors even piped; the
``NO_COLOR`` environment variable (https://no-color.org) forces
color off and beats the setting.
"""

from __future__ import annotations

import os
import sys
from typing import Callable, Mapping, TextIO

RESET = "\x1b[0m"
GREEN = "\x1b[32m"
RED = "\x1b[31m"
CYAN = "\x1b[36m"
BOLD_CYAN = "\x1b[1;36m"
DIM = "\x1b[2m"

COLOR_MODES = ("auto", "always", "never")


def should_color(mode: str, *, isatty: bool,
                 env: Mapping[str, str]) -> bool:
    """The gating decision, pure. NO_COLOR (present at all, per
    no-color.org) beats every setting; ``never`` is off; ``always``
    is on even piped; ``auto`` (and any unknown value) follows the
    TTY."""
    if "NO_COLOR" in env:
        return False
    if mode == "never":
        return False
    if mode == "always":
        return True
    return bool(isatty)


def paint(text: str, mode: str, *, stream: TextIO | None = None,
          env: Mapping[str, str] | None = None) -> str:
    """Return `text` with its diff regions colorized when the gating
    allows color for `stream` (default: the real sys.stdout at call
    time); otherwise return `text` unchanged. Pure with respect to
    the input string — callers print the result and keep using the
    original everywhere else."""
    stream = sys.stdout if stream is None else stream
    env = os.environ if env is None else env
    isatty = bool(getattr(stream, "isatty", lambda: False)())
    if not should_color(mode, isatty=isatty, env=env):
        return text
    return colorize_diff(text)


def printer(mode: str) -> Callable[[str], None]:
    """A print-compatible callable that paints before printing —
    the presenter / terminal-decider output shape for print sites
    that render for a terminal."""
    def _print(text: str = "") -> None:
        print(paint(text, mode))
    return _print


# ----------------------------------------------------------------------
# The colorizer
# ----------------------------------------------------------------------

def _split_indent(line: str) -> tuple[str, str]:
    body = line.lstrip()
    return line[:len(line) - len(body)], body


def _wrap(code: str, line: str) -> str:
    return f"{code}{line}{RESET}" if line else line


def colorize_diff(text: str) -> str:
    """Colorize the unified-diff regions in `text`, line by line.

    A region opens on a ``--- `` line immediately followed (same
    indentation) by a ``+++ `` line; both headers are painted bold
    cyan. Inside the region, lines keep the headers' indentation:
    ``@@`` hunks cyan, ``+`` additions green, ``-`` deletions red,
    ``\\`` markers (``\\ No newline at end of file``) dim, and
    space-led context lines pass through in the default color. The
    first line that doesn't fit ends the region and is reconsidered
    on its own. Text with no diff region is returned unchanged —
    byte-for-byte.
    """
    lines = text.split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        indent, body = _split_indent(line)
        if body.startswith("--- ") and i + 1 < n:
            next_indent, next_body = _split_indent(lines[i + 1])
            if next_indent == indent and next_body.startswith("+++ "):
                out.append(_wrap(BOLD_CYAN, line))
                out.append(_wrap(BOLD_CYAN, lines[i + 1]))
                i += 2
                while i < n:
                    raw = lines[i]
                    if indent:
                        if not raw.startswith(indent):
                            break
                        rest = raw[len(indent):]
                    else:
                        rest = raw
                    if rest.startswith("@@") and "@@" in rest[2:]:
                        out.append(_wrap(CYAN, raw))
                    elif rest.startswith("+"):
                        out.append(_wrap(GREEN, raw))
                    elif rest.startswith("-"):
                        out.append(_wrap(RED, raw))
                    elif rest.startswith("\\"):
                        out.append(_wrap(DIM, raw))
                    elif rest.startswith(" "):
                        out.append(raw)  # context: default color
                    else:
                        break
                    i += 1
                continue
        out.append(line)
        i += 1
    return "\n".join(out)
