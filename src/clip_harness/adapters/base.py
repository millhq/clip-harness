"""Adapter interfaces (structural — implementations duck-type these)."""

from __future__ import annotations

from typing import Protocol

from ..gate import Candidate, DecisionRecord


class Clipboard(Protocol):
    def get_text(self) -> str | None:
        """Current clipboard text, or None if it holds no text."""
        ...

    def set_text(self, text: str) -> None:
        ...


class Decider(Protocol):
    """Captures the human's review decision as explicit data.

    The candidate is a file Candidate or a CommandCandidate (a held
    shell command) — the decision vocabulary differs by kind (see
    gate.DecisionRecord). Returns None when no decision was made
    inside the window — the run then stays PENDING with its
    candidate intact.
    """

    def __call__(self, candidate: Candidate) -> DecisionRecord | None:
        ...


class DiffOpener(Protocol):
    """Presents a staged candidate for review (diffs in an editor)."""

    def __call__(self, candidate: Candidate) -> None:
        ...
