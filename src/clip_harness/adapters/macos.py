"""macOS production adapters — UNTESTED ON MAC (written on Linux).

These map the core onto the Mac's own tools, the same surfaces the
original clipboard_loop.py used:

- clipboard: the original ran inside iTerm2's Python runtime and used
  AppKit's NSPasteboard; a standalone CLI maps onto the equivalent
  pbpaste/pbcopy commands (same pasteboard).
- notifications: NOT an adapter — a notification is a hook
  (the shipped default notify hook calls osascript itself).
- review: VS Code — manifest summary file first, then `code --diff`
  per update, candidate view for creates, live file for deletes.

Ali QAs these on his Mac; the lifecycle they plug into is fully
covered by the headless tests.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..gate import Candidate


class PbClipboard:
    def get_text(self) -> str | None:
        try:
            proc = subprocess.run(["pbpaste"], capture_output=True,
                                  timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout.decode("utf-8", "replace")

    def set_text(self, text: str) -> None:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"),
                       timeout=5, check=False)


class VSCodeDiffOpener:
    """Opens the review in VS Code: the manifest summary FIRST, then a
    diff per file — Ali reviews on disk, never by reading chat text."""

    def __init__(self, code_cmd: str = "code"):
        self.code_cmd = code_cmd

    def __call__(self, candidate: Candidate) -> None:
        summary = candidate.dir / "summary.txt"
        summary.write_text(candidate.summary(), encoding="utf-8")
        self._run([str(summary)])
        for entry in candidate.entries:
            live = Path(entry.abs_path)
            staged = (candidate.dir / "candidates" / entry.candidate_file
                      if entry.candidate_file else None)
            if entry.actual_op == "UPDATE" and staged is not None:
                self._run(["--diff", str(live), str(staged)])
            elif entry.actual_op == "NEW" and staged is not None:
                self._run([str(staged)])
            elif entry.actual_op == "DELETE":
                self._run([str(live)])   # tombstone: view what's going

    def _run(self, args: list[str]) -> None:
        try:
            subprocess.run([self.code_cmd, *args], timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
