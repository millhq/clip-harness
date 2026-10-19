"""The gate: built-in policy, candidate staging, and reviewed apply.

Two layers, per the hooks report (§7):

1. Built-in policy — the harness's own checks. Hard blocks (destructive
   patterns) are evaluated BEFORE any allowlist, mirroring Claude Code's
   exit-code-2 "block before the tool runs". Denylist denies; a non-empty
   allowlist that doesn't match asks; shell-mutation heuristics ask so
   the payload is forced through the review branch.

2. The mutation branch (§7.3) — file payloads are NEVER written live.
   They are classified against disk (NEW / UPDATE / DELETE / NO-CHANGE,
   with declared-vs-actual mismatches flagged), staged as candidates
   with base hashes and a manifest, reviewed as explicit decision data
   (decision.json), and applied only for accepted entries, with
   post-apply hash verification and rollback from pre-apply snapshots
   on partial failure.

Anything richer than this (custom classifiers, extra reviewers) belongs
in a hook, not here — the core stays a thin mapping onto git/shell.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .core import Decision, FileSpec, GateVerdict
from .settings import Settings


class GateError(Exception):
    """Candidate staging failed — the gate fails closed on this."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Built-in policy
# ---------------------------------------------------------------------------

_MUTATION_PATTERNS = [
    r"(?<![\w/])rm\s", r"(?<![\w/])mv\s", r"(?<![\w/])cp\s",
    r"(?<![\w/])mkdir\b", r"(?<![\w/])touch\b", r"(?<![\w/])chmod\b",
    r"(?<![\w/])chown\b", r"(?<![\w/])ln\s+-\w*s", r"\bsed\s+[^|]*-i",
    r"\bperl\s+[^|]*-pi", r"\btee\s", r"\bdd\s+[^|]*\bof=",
    r"(?<![\w])>\s*(?!/dev/null)[^\s&|]",
]
_MUTATION_RE = re.compile("|".join(_MUTATION_PATTERNS))


def looks_like_mutation(command: str) -> bool:
    return bool(_MUTATION_RE.search(command))


def _glob_any(command: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        if fnmatch.fnmatch(command, pattern) or fnmatch.fnmatch(
                command, f"*{pattern}*"):
            return pattern
    return None


def evaluate_policy(command: str, settings: Settings) -> GateVerdict:
    """The harness's own gate decision for a shell command.

    Order matters: hard blocks first (before allowlist evaluation),
    then denylist, then protected paths, then allowlist, then the
    mutation heuristic.
    """
    for pattern in settings.hard_block_patterns:
        if re.search(pattern, command):
            return GateVerdict(
                Decision.DENY,
                f"hard block: command matches destructive pattern {pattern!r}",
                hard_block=True)

    hit = _glob_any(command, settings.denylist)
    if hit is not None:
        return GateVerdict(Decision.DENY, f"denylist match: {hit!r}")

    protected = [p for p in settings.protected_paths if p in command]
    if protected and looks_like_mutation(command):
        return GateVerdict(
            Decision.DENY,
            f"hard block: mutation touches protected path {protected[0]!r}",
            hard_block=True)

    if settings.allowlist and _glob_any(command, settings.allowlist) is None:
        return GateVerdict(
            Decision.ASK,
            "command is not on the allowlist — human gate required")

    if looks_like_mutation(command):
        if settings.mutation_gate == "deny":
            return GateVerdict(
                Decision.DENY,
                "shell mutation denied by settings (mutation_gate=deny); "
                "send file changes as ```file/```patch fences instead")
        if settings.mutation_gate == "review":
            return GateVerdict(
                Decision.ASK,
                "shell command mutates files — review gate required; "
                "prefer ```file/```patch fences so changes can be diffed")

    return GateVerdict(Decision.ALLOW)


# ---------------------------------------------------------------------------
# File classification + candidate staging (§7.3)
# ---------------------------------------------------------------------------

@dataclass
class CandidateEntry:
    path: str                    # path as declared (relative to payload cwd)
    abs_path: str
    declared_op: str             # create|update|delete|auto
    actual_op: str               # NEW|UPDATE|DELETE|NO_CHANGE
    mismatch: bool
    base_hash: str | None        # live file hash before apply (None = absent)
    candidate_hash: str | None   # staged content hash (None for deletes)
    lines_added: int
    lines_removed: int
    candidate_file: str | None   # file inside the candidate dir, or None
    snapshot_file: str | None    # pre-apply snapshot inside candidate dir


@dataclass
class Candidate:
    candidate_id: str
    run_id: str
    session_id: str | None
    dir: Path
    entries: list[CandidateEntry] = field(default_factory=list)

    def summary(self) -> str:
        """Manifest summary — shown FIRST, before any diff opens."""
        lines = [f"candidate {self.candidate_id} — "
                 f"{len(self.entries)} file(s):"]
        for e in self.entries:
            line = (f"  {e.path} · {e.actual_op} · "
                    f"+{e.lines_added}/−{e.lines_removed}")
            if e.mismatch:
                line += f"  ⚠ declared {e.declared_op}"
            lines.append(line)
        return "\n".join(lines)

    def pending(self) -> bool:
        return not (self.dir / "decision.json").exists()

    def presentation(self) -> str:
        """The terminal review surface: manifest first, then each
        file's inline diff, headed by path + op tag. (VS Code remains
        available as the deep-review option for the same candidate.)"""
        parts = [self.summary()]
        for entry in self.entries:
            diff = render_diff(self, entry)
            if diff:
                parts.append(f"  {entry.path} — {entry.actual_op}")
                parts.extend(f"    {ln}" for ln in diff.splitlines())
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Inline diff rendering — core transport, the default review surface
# ---------------------------------------------------------------------------
#
# Claude Code shows a file write/update inline; clip-harness does the
# same. Every staged change renders as a unified diff in the terminal
# review presentation AND in the result block after the decision, so
# the paste-back shows the model its own proposal exactly as rendered
# and what happened to it. Rendering is capped per file — the block
# never carries an unbounded diff; the pointer names the candidate
# directory holding both full sides.

MAX_DIFF_LINES = 80     # per-file rendered diff cap, lines
MAX_DIFF_CHARS = 6000   # per-file rendered diff cap, chars


def _entry_sides(candidate: "Candidate",
                 entry: "CandidateEntry") -> tuple[bytes | None,
                                                    bytes | None]:
    """(live bytes, proposed bytes) for one entry, from the staged
    snapshot/candidate files — so an accept-edited renders the bytes
    that were actually applied."""
    old = new = None
    if entry.snapshot_file is not None:
        old = (candidate.dir / "snapshots"
               / entry.snapshot_file).read_bytes()
    if entry.candidate_file is not None:
        new = (candidate.dir / "candidates"
               / entry.candidate_file).read_bytes()
    if entry.actual_op == "NEW":
        old = None
    elif entry.actual_op == "DELETE":
        new = None
    return old, new


def render_diff(candidate: "Candidate", entry: "CandidateEntry", *,
                max_lines: int = MAX_DIFF_LINES,
                max_chars: int = MAX_DIFF_CHARS) -> str:
    """One entry's capped unified diff ('' when there is nothing to
    show, e.g. a NO_CHANGE entry)."""
    if entry.actual_op == "NO_CHANGE":
        return ""
    old, new = _entry_sides(candidate, entry)
    old_lines = (old or b"").decode("utf-8", "replace").splitlines()
    new_lines = (new or b"").decode("utf-8", "replace").splitlines()
    lines = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile="/dev/null" if old is None else f"a/{entry.path}",
        tofile="/dev/null" if new is None else f"b/{entry.path}",
        lineterm="", n=3))
    if not lines:
        return ""
    total = len(lines)
    shown = lines
    truncated = False
    if len(shown) > max_lines:
        shown = shown[:max_lines]
        truncated = True
    while shown and len("\n".join(shown)) > max_chars:
        shown = shown[:-1]
        truncated = True
    out = list(shown)
    if truncated:
        out.append(f"… {total - len(shown)} more lines — "
                   f"full diff in {candidate.dir}")
    return "\n".join(out)


def _count_diff(old: bytes | None, new: bytes | None) -> tuple[int, int]:
    old_lines = (old or b"").decode("utf-8", "replace").splitlines()
    new_lines = (new or b"").decode("utf-8", "replace").splitlines()
    added = removed = 0
    for line in difflib.unified_diff(old_lines, new_lines, lineterm=""):
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def _apply_patch(live: bytes | None, patch_text: str, rel_path: str) -> bytes:
    """Turn a unified diff into candidate content by delegating to git —
    the core maps onto the known tool instead of reimplementing patching.
    """
    with tempfile.TemporaryDirectory(prefix="clip-harness-patch-") as tmp:
        root = Path(tmp)
        target = root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if live is not None:
            target.write_bytes(live)
        patch_file = root / "change.patch"
        # git apply requires newline-terminated lines; the payload
        # parser strips the fence's trailing newline, so restore it.
        if not patch_text.endswith("\n"):
            patch_text += "\n"
        patch_file.write_text(patch_text, encoding="utf-8")
        for strip in (None, "-p0"):
            cmd = ["git", "apply", "--whitespace=nowarn"]
            if strip:
                cmd.append(strip)
            cmd.append(str(patch_file))
            proc = subprocess.run(cmd, cwd=root, capture_output=True,
                                  text=True, timeout=30)
            if proc.returncode == 0:
                return target.read_bytes()
        raise GateError(
            f"patch for {rel_path} does not apply cleanly: "
            f"{proc.stderr.strip()[:300]}")


def classify_and_stage(candidate_dir: Path, run_id: str,
                       session_id: str | None,
                       specs: list[FileSpec], cwd: Path) -> Candidate:
    """Stage every file spec as a candidate. Never writes live files.

    A spec whose resolved target escapes the working directory
    (``../`` traversal, an absolute path elsewhere) is rejected —
    GateError, fail closed — before anything is staged.
    """
    candidate_dir.mkdir(parents=True, exist_ok=True)
    (candidate_dir / "candidates").mkdir(exist_ok=True)
    (candidate_dir / "snapshots").mkdir(exist_ok=True)
    candidate = Candidate(candidate_id=candidate_dir.name, run_id=run_id,
                          session_id=session_id, dir=candidate_dir)
    cwd_root = cwd.resolve()

    for index, spec in enumerate(specs):
        abs_path = (cwd / spec.path).resolve()
        if not abs_path.is_relative_to(cwd_root):
            raise GateError(
                f"file target escapes the working directory: "
                f"{spec.path!r}")
        live = abs_path.read_bytes() if abs_path.is_file() else None
        base_hash = sha256_bytes(live) if live is not None else None

        if spec.declared_op == "delete":
            candidate_bytes = None
        elif spec.is_patch:
            candidate_bytes = _apply_patch(live, spec.patch or "", spec.path)
        else:
            candidate_bytes = (spec.content or "").encode("utf-8")

        if live is None and candidate_bytes is None:
            actual = "NO_CHANGE"
        elif live is None:
            actual = "NEW"
        elif candidate_bytes is None:
            actual = "DELETE"
        elif live == candidate_bytes:
            actual = "NO_CHANGE"
        else:
            actual = "UPDATE"

        declared_expect = {"create": "NEW", "update": "UPDATE",
                           "delete": "DELETE"}.get(spec.declared_op)
        mismatch = (declared_expect is not None
                    and actual != declared_expect)

        added, removed = _count_diff(
            live if actual != "NEW" else None,
            candidate_bytes if actual != "DELETE" else None)
        if actual == "NEW":
            added, removed = _count_diff(None, candidate_bytes)
        elif actual == "DELETE":
            added, removed = _count_diff(live, None)

        candidate_file = snapshot_file = None
        if candidate_bytes is not None and actual != "NO_CHANGE":
            cf = candidate_dir / "candidates" / str(index)
            cf.write_bytes(candidate_bytes)
            candidate_file = cf.name
        if live is not None and actual in ("UPDATE", "DELETE"):
            sf = candidate_dir / "snapshots" / str(index)
            sf.write_bytes(live)
            snapshot_file = sf.name

        candidate.entries.append(CandidateEntry(
            path=spec.path, abs_path=str(abs_path),
            declared_op=spec.declared_op,
            actual_op=actual, mismatch=mismatch, base_hash=base_hash,
            candidate_hash=(sha256_bytes(candidate_bytes)
                            if candidate_bytes is not None else None),
            lines_added=added, lines_removed=removed,
            candidate_file=candidate_file, snapshot_file=snapshot_file))

    manifest = {
        "candidate_id": candidate.candidate_id, "run_id": run_id,
        "session_id": session_id,
        "entries": [asdict(e) for e in candidate.entries],
    }
    (candidate_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    return candidate


def load_candidate(candidate_dir: Path) -> Candidate:
    manifest = json.loads(
        (candidate_dir / "manifest.json").read_text(encoding="utf-8"))
    entries = [CandidateEntry(**e) for e in manifest["entries"]]
    return Candidate(candidate_id=manifest["candidate_id"],
                     run_id=manifest["run_id"],
                     session_id=manifest.get("session_id"),
                     dir=candidate_dir, entries=entries)


# ---------------------------------------------------------------------------
# Decision data + apply
# ---------------------------------------------------------------------------

@dataclass
class DecisionRecord:
    decision: str                 # accept | accept-edited | reject
    reason: str = ""
    accepted_files: list[str] | None = None   # None = all entries
    decided_at: str = ""

    def accepts(self, entry: CandidateEntry) -> bool:
        if self.decision not in ("accept", "accept-edited"):
            return False
        if self.accepted_files is None:
            return True
        return entry.path in self.accepted_files


def write_decision(candidate: Candidate, record: DecisionRecord) -> Path:
    path = candidate.dir / "decision.json"
    path.write_text(json.dumps(asdict(record), indent=2), encoding="utf-8")
    return path


def read_decision(candidate_dir: Path) -> DecisionRecord | None:
    path = candidate_dir / "decision.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return DecisionRecord(**data)


@dataclass
class ApplyEntryResult:
    path: str
    op: str                       # NEW|UPDATE|DELETE|NO_CHANGE
    status: str                   # applied|rejected|skipped|failed|rolled-back
    reviewer_modified: bool = False
    detail: str = ""


@dataclass
class ApplyReport:
    entries: list[ApplyEntryResult] = field(default_factory=list)
    rolled_back: bool = False
    # path -> capped inline diff, rendered from the staged candidate
    # at apply time (so accept-edited shows the bytes actually applied)
    diffs: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(e.status != "failed" for e in self.entries)

    def outcome_lines(self) -> list[str]:
        """Lines for the result block — with NEW/UPDATED tags (§7.3)."""
        tag = {"NEW": "NEW", "UPDATE": "UPDATED", "DELETE": "DELETED"}
        lines = []
        for e in self.entries:
            if e.status == "applied":
                lines.append(f"  ✓ {e.path} — {tag.get(e.op, e.op)}"
                             + (" (edited in review)"
                                if e.reviewer_modified else ""))
            elif e.status == "rejected":
                lines.append(f"  ✗ {e.path} — rejected")
            elif e.status == "skipped":
                lines.append(f"  · {e.path} — no change")
            elif e.status == "rolled-back":
                lines.append(f"  ↩ {e.path} — rolled back")
            else:
                lines.append(f"  ✗ {e.path} — FAILED: {e.detail}")
        return lines

    def section(self) -> str:
        """The result-block files section: the outcome line per entry,
        each followed by its inline diff — applied and rejected alike,
        so the block shows the proposal exactly as rendered and what
        happened to it."""
        lines = ["files:"]
        for entry, line in zip(self.entries, self.outcome_lines()):
            lines.append(line)
            diff = self.diffs.get(entry.path)
            if diff:
                lines.extend(f"    {ln}" for ln in diff.splitlines())
        return "\n".join(lines)


def apply_candidate(candidate: Candidate,
                    decision: DecisionRecord) -> ApplyReport:
    """Apply only accepted entries; verify hashes; roll back on failure.

    The candidate file's CURRENT bytes are what get applied — for
    accept-edited the reviewer may have changed them in the editor, so
    the applied hash is recomputed, never assumed from the manifest.
    """
    report = ApplyReport()
    report.diffs = {e.path: render_diff(candidate, e)
                    for e in candidate.entries}
    applied: list[CandidateEntry] = []

    for entry in candidate.entries:
        if entry.actual_op == "NO_CHANGE":
            report.entries.append(ApplyEntryResult(
                entry.path, entry.actual_op, "skipped"))
            continue
        if not decision.accepts(entry):
            report.entries.append(ApplyEntryResult(
                entry.path, entry.actual_op, "rejected"))
            continue

        target = Path(entry.abs_path)
        try:
            # Base must not have moved since staging.
            live_now = target.read_bytes() if target.is_file() else None
            live_hash = sha256_bytes(live_now) if live_now is not None else None
            if live_hash != entry.base_hash:
                raise GateError(
                    f"{entry.path} changed on disk since staging "
                    f"(base hash mismatch)")

            if entry.actual_op == "DELETE":
                target.unlink()
                if target.exists():
                    raise GateError(f"{entry.path}: delete did not stick")
                applied.append(entry)
                report.entries.append(ApplyEntryResult(
                    entry.path, entry.actual_op, "applied"))
            else:
                staged = (candidate.dir / "candidates"
                          / entry.candidate_file).read_bytes()
                reviewer_modified = (
                    sha256_bytes(staged) != entry.candidate_hash)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(staged)
                if sha256_bytes(target.read_bytes()) != sha256_bytes(staged):
                    raise GateError(
                        f"{entry.path}: post-apply hash verification failed")
                applied.append(entry)
                report.entries.append(ApplyEntryResult(
                    entry.path, entry.actual_op, "applied",
                    reviewer_modified=reviewer_modified))
        except (OSError, GateError) as exc:
            report.entries.append(ApplyEntryResult(
                entry.path, entry.actual_op, "failed", detail=str(exc)))
            _rollback(candidate, applied, report)
            break

    return report


def _rollback(candidate: Candidate, applied: list[CandidateEntry],
              report: ApplyReport) -> None:
    """Restore applied entries from their pre-apply snapshots."""
    report.rolled_back = True
    by_path = {e.path: e for e in report.entries}
    for entry in reversed(applied):
        target = Path(entry.abs_path)
        try:
            if entry.snapshot_file is not None:
                snapshot = (candidate.dir / "snapshots"
                            / entry.snapshot_file).read_bytes()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(snapshot)
            elif target.exists():
                target.unlink()          # it was a create — remove it
            if entry.path in by_path:
                by_path[entry.path].status = "rolled-back"
        except OSError:
            if entry.path in by_path:
                by_path[entry.path].status = "failed"
                by_path[entry.path].detail += " (rollback also failed)"
