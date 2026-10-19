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


def _mask_quoted(command: str) -> str:
    """The command with quoted contents blanked to spaces.

    Length and positions are preserved; the quote characters
    themselves stay. Shell metacharacters inside quotes are data,
    not syntax — an awk comparison like 'NR>1' is not a redirect —
    so the mutation patterns scan this copy and only real shell
    syntax can match. Backslash escapes are honoured inside double
    quotes and outside quotes (an escaped quote opens nothing);
    inside single quotes a backslash is literal, as in sh. An
    unterminated quote masks to the end of the string.
    """
    out = list(command)
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "\\":
            i += 2  # escaped char (maybe a quote) is not syntax
            continue
        if ch not in ("'", '"'):
            i += 1
            continue
        quote = ch
        i += 1
        while i < n:
            ch = command[i]
            if quote == '"' and ch == "\\" and i + 1 < n:
                out[i] = out[i + 1] = " "
                i += 2
                continue
            if ch == quote:
                break
            out[i] = " "
            i += 1
        i += 1  # the closing quote (or one past the end)
    return "".join(out)


def _quoted_span(text: str, start: int) -> tuple[str, int]:
    """(contents, end index) of the quoted span opening at `start`.

    Double quotes honour backslash escapes (the escaped char is what
    the inner shell sees); single quotes are literal. An
    unterminated span runs to the end of the string.
    """
    quote = text[start]
    chars: list[str] = []
    i = start + 1
    while i < len(text):
        ch = text[i]
        if quote == '"' and ch == "\\" and i + 1 < len(text):
            chars.append(text[i + 1])
            i += 2
            continue
        if ch == quote:
            return "".join(chars), i + 1
        chars.append(ch)
        i += 1
    return "".join(chars), len(text)


# A nested shell invocation: bash/sh/zsh/... with a -c flag (possibly
# bundled, e.g. -lc), then the command string as the next argument.
_SHELL_C_ARG = re.compile(
    r"(?:^|[\s;&|])\s*(?:\S*/)?(?:bash|zsh|dash|ksh|sh)"
    r"(?:\s+-[A-Za-z]+)*\s+-[A-Za-z]*c[A-Za-z]*\s+")


def _shell_c_arguments(command: str) -> list[str]:
    """The command strings handed to nested `sh -c '...'` shells.

    Those quotes belong to the OUTER shell — the argument is a
    command line in its own right, so the mutation scan must see
    through it (`bash -c 'rm x'` mutates even though `rm` sits inside
    quotes from the outer command's point of view).
    """
    args: list[str] = []
    for match in _SHELL_C_ARG.finditer(command):
        i = match.end()
        if i >= len(command):
            break
        if command[i] in ("'", '"'):
            arg, _end = _quoted_span(command, i)
            args.append(arg)
        else:
            word = re.match(r"[^\s;&|]+", command[i:])
            if word:
                args.append(word.group(0))
    return args


_MAX_C_DEPTH = 4


def looks_like_mutation(command: str, _depth: int = 0) -> bool:
    """Heuristic: does this shell command write to files?

    Scans a quote-masked copy of the command, plus — recursively —
    the argument of any nested `sh -c` invocation, whose own quoted
    spans are masked at its own level in turn.
    """
    if _MUTATION_RE.search(_mask_quoted(command)):
        return True
    if _depth >= _MAX_C_DEPTH:
        return False
    return any(looks_like_mutation(arg, _depth + 1)
               for arg in _shell_c_arguments(command))


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


# ---------------------------------------------------------------------------
# Patch targeting — `path:` is the authoritative target
# ---------------------------------------------------------------------------
#
# A patch payload names its target with `path:`; the unified diff's
# ---/+++ header filenames are the model's own bookkeeping (repo-
# relative a//b/ paths, basenames, absolute paths — whichever it
# produced from wherever it was standing). They must agree with
# `path:`, but they never DIRECT the apply: headers are normalized
# to the target's basename and git applies the diff to the live
# bytes under that name, so neither the harness process cwd nor the
# header shape can steer the change to a different file.

_HUNK_HEADER_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _header_name(raw: str) -> str:
    """The filename from a ---/+++ header line's remainder: drop any
    tab-separated timestamp, and C-style quoting git adds around
    names with special characters."""
    name = raw.split("\t")[0].strip()
    if len(name) >= 2 and name.startswith('"') and name.endswith('"'):
        name = name[1:-1]
    return name


def _patch_sections(patch_text: str) -> list[tuple[int, str, str]]:
    """(line index, old name, new name) per file section in a diff.

    Header pairs are ---/+++ line pairs found OUTSIDE hunk bodies;
    hunk bodies are consumed by the line counts their @@ headers
    declare, so a removed line whose content starts with '--' is
    never mistaken for a file header.
    """
    lines = patch_text.splitlines()
    sections: list[tuple[int, str, str]] = []
    i = 0
    while i < len(lines):
        hunk = _HUNK_HEADER_RE.match(lines[i])
        if hunk:
            old_left = int(hunk.group(2)) if hunk.group(2) else 1
            new_left = int(hunk.group(4)) if hunk.group(4) else 1
            i += 1
            while i < len(lines) and (old_left > 0 or new_left > 0):
                line = lines[i]
                if line.startswith("\\"):
                    pass  # "\ No newline at end of file"
                elif line.startswith("-"):
                    old_left -= 1
                elif line.startswith("+"):
                    new_left -= 1
                else:  # context (incl. a bare empty line)
                    old_left -= 1
                    new_left -= 1
                i += 1
            continue
        if lines[i].startswith("--- ") and i + 1 < len(lines) \
                and lines[i + 1].startswith("+++ "):
            sections.append((i, _header_name(lines[i][4:]),
                             _header_name(lines[i + 1][4:])))
            i += 2
            continue
        i += 1
    return sections


def _strip_ab_prefix(name: str) -> str:
    if name.startswith(("a/", "b/")):
        return name[2:]
    return name


def _header_matches_target(name: str, spec_path: str,
                           abs_path: Path) -> bool:
    """Is a ---/+++ header name a plausible reference to the
    authoritative target? Consistent iff, modulo a conventional
    a//b/ prefix, it equals the declared path, equals the resolved
    target, is a repo-relative suffix of the resolved target, or is
    the target's basename. Anything else names a different file."""
    bare = _strip_ab_prefix(name)
    abs_str = str(abs_path)
    return (name == spec_path or bare == spec_path
            or name == abs_str or bare == abs_str
            or abs_str.endswith("/" + bare)
            or abs_str.endswith("/" + name)
            or bare == abs_path.name)


def _validate_patch_target(patch_text: str, spec_path: str,
                           abs_path: Path) -> list[tuple[int, str, str]]:
    """The patch's header sections, checked against `path:`.

    Fails closed (GateError) when the patch carries no ---/+++
    headers at all, when a header names a genuinely different
    file, or when --- and +++ disagree with each other. A /dev/null
    side (creation / deletion) needs no check; the other side must
    still name the target.
    """
    sections = _patch_sections(patch_text)
    if not sections:
        raise GateError(
            f"patch for {spec_path} has no ---/+++ file headers — "
            "cannot establish that it targets that file")
    for _index, old, new in sections:
        for name in (old, new):
            if name == "/dev/null":
                continue
            if not _header_matches_target(name, spec_path, abs_path):
                raise GateError(
                    f"patch for {spec_path} names a different file "
                    f"in its headers: {name!r}")
        if old != "/dev/null" and new != "/dev/null":
            old_bare = _strip_ab_prefix(old)
            new_bare = _strip_ab_prefix(new)
            if old_bare != new_bare \
                    and not old_bare.endswith("/" + new_bare) \
                    and not new_bare.endswith("/" + old_bare):
                raise GateError(
                    f"patch for {spec_path} has disagreeing headers: "
                    f"{old!r} vs {new!r}")
    return sections


def _normalize_patch(patch_text: str,
                     sections: list[tuple[int, str, str]],
                     abs_path: Path) -> str:
    """The working copy of the patch with every header rewritten to
    the target's basename under conventional a//b/ prefixes
    (/dev/null sides preserved) — the form git applies, from the
    scratch dir holding the target under that basename."""
    base = abs_path.name
    lines = patch_text.splitlines()
    for index, old, new in sections:
        lines[index] = ("--- /dev/null" if old == "/dev/null"
                        else f"--- a/{base}")
        lines[index + 1] = ("+++ /dev/null" if new == "/dev/null"
                            else f"+++ b/{base}")
    for index, line in enumerate(lines):
        if line.startswith("diff --git "):
            lines[index] = f"diff --git a/{base} b/{base}"
    text = "\n".join(lines)
    # git apply requires newline-terminated lines; the payload
    # parser strips the fence's trailing newline, so restore it.
    return text if text.endswith("\n") else text + "\n"


def _apply_patch(live: bytes | None, patch_text: str, spec_path: str,
                 abs_path: Path) -> bytes | None:
    """Turn a unified diff into candidate content by delegating to
    git — the core maps onto the known tool instead of
    reimplementing patching.

    `path:` (resolved to `abs_path`) is the authoritative target:
    the diff's headers are validated against it and normalized to
    its basename before git sees them, and application runs in a
    scratch dir holding the live bytes under that basename — the
    harness process cwd is irrelevant. Returns the post-patch
    bytes, or None when the patch deletes the file.
    """
    sections = _validate_patch_target(patch_text, spec_path, abs_path)
    normalized = _normalize_patch(patch_text, sections, abs_path)
    with tempfile.TemporaryDirectory(prefix="clip-harness-patch-") as tmp:
        root = Path(tmp)
        target = root / abs_path.name
        if live is not None:
            target.write_bytes(live)
        patch_file = root / "change.patch"
        patch_file.write_text(normalized, encoding="utf-8")
        proc = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", str(patch_file)],
            cwd=root, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise GateError(
                f"patch for {spec_path} does not apply cleanly: "
                f"{proc.stderr.strip()[:300]}")
        if not target.exists():
            return None  # the patch deleted the file
        return target.read_bytes()


def classify_and_stage(candidate_dir: Path, run_id: str,
                       session_id: str | None,
                       specs: list[FileSpec], cwd: Path) -> Candidate:
    """Stage every file spec as a candidate. Never writes live files.

    A spec whose RELATIVE path resolves outside the working
    directory (``../`` traversal) is rejected — GateError, fail
    closed — before anything is staged. An absolute path names its
    target explicitly and stages like any other target, behind the
    same review gate.
    """
    candidate_dir.mkdir(parents=True, exist_ok=True)
    (candidate_dir / "candidates").mkdir(exist_ok=True)
    (candidate_dir / "snapshots").mkdir(exist_ok=True)
    candidate = Candidate(candidate_id=candidate_dir.name, run_id=run_id,
                          session_id=session_id, dir=candidate_dir)
    cwd_root = cwd.resolve()

    for index, spec in enumerate(specs):
        abs_path = (cwd / spec.path).resolve()
        if not Path(spec.path).is_absolute() \
                and not abs_path.is_relative_to(cwd_root):
            raise GateError(
                f"file target escapes the working directory: "
                f"{spec.path!r}")
        live = abs_path.read_bytes() if abs_path.is_file() else None
        base_hash = sha256_bytes(live) if live is not None else None

        if spec.declared_op == "delete":
            candidate_bytes = None
        elif spec.is_patch:
            candidate_bytes = _apply_patch(live, spec.patch or "",
                                           spec.path, abs_path)
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
