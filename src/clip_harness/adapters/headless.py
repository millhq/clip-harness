"""Headless adapters — the QA surface.

These stand-ins let the full lifecycle run anywhere (Linux CI, a demo,
an agent playing the Copilot role): the "clipboard" is a file, and a
review decision is supplied by dropping a decision.json into the
candidate directory — the exact same file and schema the production
flow uses. (Notifications need no stand-in: they are hooks, and a
test can wire a hook command that appends to a file.) Nothing here is
a mock of the core; only the OS surfaces are substituted.
"""

from __future__ import annotations

import time
from pathlib import Path

from ..gate import Candidate, DecisionRecord, read_decision


class MemoryClipboard:
    """In-process clipboard for unit tests."""

    def __init__(self, text: str = ""):
        self.text = text
        self.history: list[str] = []

    def get_text(self) -> str | None:
        return self.text

    def set_text(self, text: str) -> None:
        self.text = text
        self.history.append(text)


class FileClipboard:
    """Clipboard backed by a file — write a payload in, read the
    result block out. This is the headless demo's clipboard."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def get_text(self) -> str | None:
        try:
            return self.path.read_text(encoding="utf-8")
        except OSError:
            return None

    def set_text(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")


class FileDropDecider:
    """Review decision supplied by dropping decision.json into the
    candidate directory — the same file the production flow reads.

    Polls for the file up to timeout_s; no file means no decision,
    so the run goes PENDING exactly as it would on the Mac.
    """

    def __init__(self, timeout_s: float = 0.0, poll_s: float = 0.05):
        self.timeout_s = timeout_s
        self.poll_s = poll_s

    def __call__(self, candidate: Candidate) -> DecisionRecord | None:
        deadline = time.monotonic() + self.timeout_s
        while True:
            record = read_decision(candidate.dir)
            if record is not None:
                return record
            if time.monotonic() >= deadline:
                return None
            time.sleep(self.poll_s)


class StaticDecider:
    """A decider with a pre-baked answer — demos and tests."""

    def __init__(self, record: DecisionRecord | None):
        self.record = record

    def __call__(self, candidate: Candidate) -> DecisionRecord | None:
        return self.record


class TerminalDecider:
    """Interactive terminal review: manifest summary first, then an
    explicit decision. Used by the local watch / run commands on any
    platform with a tty; EOF or an empty answer means no decision
    (the run stays pending — never inferred).
    """

    def __init__(self, input_fn=input, out=print):
        self.input_fn = input_fn
        self.out = out

    def __call__(self, candidate: Candidate) -> DecisionRecord | None:
        self.out(candidate.summary())
        try:
            answer = self.input_fn(
                "[a]ccept / accept-[e]dited / [r]eject / "
                "Enter = decide later: ").strip().lower()
        except EOFError:
            return None
        if answer in ("a", "accept"):
            decision = "accept"
        elif answer in ("e", "edited", "accept-edited"):
            decision = "accept-edited"
        elif answer in ("r", "reject"):
            decision = "reject"
        else:
            return None
        reason = ""
        accepted_files = None
        if decision == "reject":
            reason = self.input_fn("reason (optional): ").strip()
        elif len(candidate.entries) > 1:
            picked = self.input_fn(
                "files to apply (Enter = all, or comma-separated paths): "
            ).strip()
            if picked:
                accepted_files = [p.strip() for p in picked.split(",")
                                  if p.strip()]
        return DecisionRecord(decision=decision, reason=reason,
                              accepted_files=accepted_files,
                              decided_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
