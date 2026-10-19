"""macOS production adapters — UNTESTED ON MAC (written on Linux).

These map the core onto the Mac's own tools, the same surfaces the
original clipboard_loop.py used:

- clipboard: the original ran inside iTerm2's Python runtime and used
  AppKit's NSPasteboard; a standalone CLI maps onto the equivalent
  pbpaste/pbcopy commands (same pasteboard).
- notifications: NOT an adapter — a notification is a hook
  (the shipped default notify hook calls osascript itself).
- review: VS Code — one waiting `code --wait` over the manifest
  summary plus `code --diff` per update, candidate view for creates,
  live file for deletes. Save + close in the editor is the decision
  (accept-edited); close without saving decides nothing.

Ali QAs these on his Mac; the lifecycle they plug into is fully
covered by the headless tests.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from ..gate import Candidate


def _hash_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


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
    """The deep-review option: opens the staged candidate in VS Code —
    the manifest summary FIRST, then a diff per file — and WAITS
    (`code --wait`) for the human to finish. The default review
    surface is the inline diff in the terminal; this is for when a
    change deserves the full editor.

    Save is the decision gesture: the editable side of every diff is
    the staged copy itself, so when VS Code hands control back the
    staged copies are re-hashed — if any one's content changed, the
    view session produced edits (``__call__`` returns True) and the
    caller concludes the review as accept-edited; if nothing changed
    the view was read-only and returns False. Comparison is by
    content hash, never mtime: a no-op save rewrites identical bytes
    and does not register. Delete entries carry no staged copy and
    are excluded from the check — touching a delete's live file
    while viewing is not an edit decision. If the waiting invocation
    itself fails (no usable `code`, `--wait` unsupported), the open
    falls back to fire-and-forget and reports no edits: a review is
    never crashed or held hostage by the editor.
    """

    def __init__(self, code_cmd: str = "code"):
        self.code_cmd = code_cmd

    def __call__(self, candidate: Candidate) -> bool:
        watched = self._watched_staged(candidate)
        before = {path: _hash_file(path) for path in watched}
        summary = candidate.dir / "summary.txt"
        summary.write_text(candidate.summary(), encoding="utf-8")
        per_entry = self._entry_args(candidate)
        args = [str(summary)] + [a for entry_args in per_entry
                                  for a in entry_args]
        if not self._wait(args):
            self._open_fire_and_forget(summary, per_entry)
            return False
        return any(_hash_file(path) != before[path] for path in watched)

    @staticmethod
    def _watched_staged(candidate: Candidate) -> list[Path]:
        """Staged copies whose content a save may change — every
        entry with staged bytes except deletes (a delete has no
        content to edit; its live file is view-only)."""
        return [candidate.dir / "candidates" / entry.candidate_file
                for entry in candidate.entries
                if entry.candidate_file is not None
                and entry.actual_op != "DELETE"]

    @staticmethod
    def _entry_args(candidate: Candidate) -> list[list[str]]:
        """Per-entry editor arguments: a `--diff` pair per update
        (staged copy on the editable right side), the staged copy
        alone per create, the live file per delete."""
        per_entry: list[list[str]] = []
        for entry in candidate.entries:
            live = Path(entry.abs_path)
            staged = (candidate.dir / "candidates" / entry.candidate_file
                      if entry.candidate_file else None)
            if entry.actual_op == "UPDATE" and staged is not None:
                per_entry.append(["--diff", str(live), str(staged)])
            elif entry.actual_op == "NEW" and staged is not None:
                per_entry.append([str(staged)])
            elif entry.actual_op == "DELETE":
                # tombstone: view what's going
                per_entry.append([str(live)])
        return per_entry

    def _wait(self, args: list[str]) -> bool:
        """Run `code --wait …`; True when the editor session ran and
        exited cleanly. No timeout: the human decides when the
        session ends — a failed launch or nonzero exit is what the
        fallback is for, not a clock."""
        try:
            proc = subprocess.run([self.code_cmd, "--wait", *args],
                                  check=False)
        except OSError:
            return False
        return proc.returncode == 0

    def _open_fire_and_forget(self, summary: Path,
                              per_entry: list[list[str]]) -> None:
        self._run([str(summary)])
        for entry_args in per_entry:
            self._run(entry_args)

    def _run(self, args: list[str]) -> None:
        try:
            subprocess.run([self.code_cmd, *args], timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
