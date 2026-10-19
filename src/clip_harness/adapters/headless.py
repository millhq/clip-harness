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

from ..gate import (Candidate, CommandCandidate, DecisionRecord,
                    derive_allow_pattern, read_decision)


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
    explicit decision. The terminal is the default review surface
    wherever a human is in the loop; when a diff opener is wired in,
    the prompt loop also offers a VS Code view of the candidate —
    it opens only when the human picks it, and the opener waits for
    the editor. Viewing alone is not a decision (the prompt
    repeats), but SAVE is: if the opener reports the staged copies
    changed in the editor, the review concludes on the spot as
    accept-edited — the apply then sources those edited staged
    bytes through the ordinary accept-edited channel, for the whole
    candidate (per-file picks stay a typed-prompt capability). EOF
    or an empty answer means no decision (the run stays pending —
    never inferred).
    """

    def __init__(self, input_fn=input, out=print, diff_opener=None):
        self.input_fn = input_fn
        self.out = out
        self.diff_opener = diff_opener

    def __call__(self, candidate: Candidate) -> DecisionRecord | None:
        self.out(candidate.presentation())
        prompt = ("[a]ccept / accept-[e]dited / [r]eject / "
                  + ("[v]iew in VS Code "
                     "(save + close = apply your edits) / "
                     if self.diff_opener is not None else "")
                  + "Enter = decide later: ")
        decision = None
        while decision is None:
            try:
                answer = self.input_fn(prompt).strip().lower()
            except EOFError:
                return None
            if answer in ("a", "accept"):
                decision = "accept"
            elif answer in ("e", "edited", "accept-edited"):
                decision = "accept-edited"
            elif answer in ("r", "reject"):
                decision = "reject"
            elif answer in ("v", "view", "vscode") \
                    and self.diff_opener is not None:
                if self.diff_opener(candidate):
                    # The human saved edits in VS Code: that IS the
                    # decision — accept the candidate with them, no
                    # re-prompt. An opener that reports nothing
                    # (read-only view, legacy None) loops as before.
                    return DecisionRecord(
                        decision="accept-edited", reason="",
                        accepted_files=None,
                        decided_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
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


class TerminalCommandDecider:
    """Interactive terminal review of a held shell COMMAND (a
    CommandCandidate): the presentation shows the full command, its
    scope and the gate's reason, then an explicit decision:

    - accept            — run it under the Seatbelt sandbox;
    - accept unsandboxed — run it bare (offered only when the
      `shell_sandbox` setting is "optional"; "strict" removes it);
    - always allow      — accept sandboxed AND persist the shown
      allowlist pattern to the project settings, so this command
      shape is not held again;
    - reject (+reason)  — nothing runs; the reason goes back to the
      model in the denial result;
    - Enter / EOF       — no decision: the command stays parked as a
      candidate (never inferred).
    """

    def __init__(self, input_fn=input, out=print,
                 shell_sandbox: str = "optional"):
        self.input_fn = input_fn
        self.out = out
        self.shell_sandbox = shell_sandbox

    def __call__(self, candidate: CommandCandidate) -> DecisionRecord | None:
        self.out(candidate.presentation(self.shell_sandbox))
        pattern = derive_allow_pattern(candidate.command)
        options = ["[a]ccept sandboxed"]
        if self.shell_sandbox != "strict":
            options.append("accept [u]nsandboxed")
        if pattern:
            options.append(f"al[w]ays allow '{pattern}'")
        options.append("[r]eject")
        prompt = " / ".join(options) + " / Enter = decide later: "
        try:
            answer = self.input_fn(prompt).strip().lower()
        except EOFError:
            return None
        if answer in ("a", "accept"):
            decision = "accept"
        elif answer in ("u", "unsandboxed", "accept-unsandboxed") \
                and self.shell_sandbox != "strict":
            decision = "accept-unsandboxed"
        elif answer in ("w", "always", "always-allow") and pattern:
            decision = "always-allow"
        elif answer in ("r", "reject"):
            decision = "reject"
        else:
            return None
        reason = ""
        if decision == "reject":
            try:
                reason = self.input_fn("reason (optional): ").strip()
            except EOFError:
                reason = ""
        return DecisionRecord(decision=decision, reason=reason,
                              decided_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
