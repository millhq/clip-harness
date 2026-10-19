"""The VS Code save gesture: picking [v]iew in the terminal review
opens the candidate in VS Code and WAITS (`code --wait`). Save is
the decision — if a staged copy's content changed when the editor
closes, the review concludes immediately as accept-edited and the
apply sources the edited staged bytes; close without saving is a
read-only view and the prompt repeats. Detection is by content
hash (a no-op save does not register), delete entries are excluded,
and a broken `code` falls back to fire-and-forget + re-prompt.

A fake `code` executable on PATH stands in for the real editor; its
behaviour is keyed on FAKE_CODE_MODE:

- (unset)   exit 0, touch nothing — a read-only view
- edit      append a line to every editable staged copy (each
            `--diff` right side, each lone staged file)
- noop      rewrite identical bytes to those same targets
- touch-live  append a line to viewed LIVE files outside the
            candidate dir (a delete's target — must NOT register)
- fail      exit 3 on every invocation — the opener must fall back
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from clip_harness.adapters.headless import TerminalDecider
from clip_harness.adapters.macos import VSCodeDiffOpener
from clip_harness.core import FileSpec, RunState
from clip_harness.gate import (apply_candidate, classify_and_stage,
                               read_decision, write_decision)

FAKE_CODE = '''#!/usr/bin/env python3
"""Fake `code`: honours --wait by 'editing' per FAKE_CODE_MODE."""
import os
import sys
from pathlib import Path


def _targets(argv):
    """(staged copies, live view files) among the editor arguments."""
    staged, live = [], []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--diff":
            staged.append(argv[i + 2])       # editable right side
            i += 3
        elif arg.startswith("--"):
            i += 1
        else:
            path = Path(arg)
            if "candidates" in path.parts:
                staged.append(arg)            # a create, opened alone
            elif path.name != "summary.txt":
                live.append(arg)              # a delete's live file
            i += 1
    return staged, live


def main():
    mode = os.environ.get("FAKE_CODE_MODE", "")
    if mode == "fail":
        return 3
    staged, live = _targets(sys.argv[1:])
    if mode == "edit":
        for name in staged:
            p = Path(name)
            p.write_bytes(p.read_bytes() + b"edited in VS Code\\n")
    elif mode == "noop":
        for name in staged:
            p = Path(name)
            p.write_bytes(p.read_bytes())     # no-op save: same bytes
    elif mode == "touch-live":
        for name in live:
            p = Path(name)
            p.write_bytes(p.read_bytes() + b"touched while viewing\\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


@pytest.fixture
def fake_code(tmp_path, monkeypatch):
    """Install the fake `code` on PATH; returns a mode setter."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "code"
    script.write_text(FAKE_CODE, encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv(
        "PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.delenv("FAKE_CODE_MODE", raising=False)
    return lambda mode: monkeypatch.setenv("FAKE_CODE_MODE", mode)


class Answers:
    """Scripted terminal input; exhaustion answers '' (defer), so a
    wrongful re-prompt shows up as a defer, never an exception."""

    def __init__(self, items):
        self.items = list(items)
        self.taken: list[str] = []

    def __call__(self, prompt=""):
        answer = self.items.pop(0) if self.items else ""
        self.taken.append(answer)
        return answer


def _stage(tmp_path, specs, live_files):
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    for name, text in live_files.items():
        path = work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    candidate = classify_and_stage(tmp_path / "cand", "r-test", None,
                                   specs, work)
    return work, candidate


def _decider(answers, opener):
    return TerminalDecider(input_fn=answers, out=lambda _s: None,
                           diff_opener=opener)


# -- the gesture, end to end ---------------------------------------------

def test_save_in_editor_concludes_run_accept_edited(
        make_runner, tmp_path, fake_code):
    """Variant A through the whole trigger-inline path: one [v]
    answer, the editor saves an extra line into the staged copy,
    and the run completes as accept-edited — applied content is the
    edited staged content, decision.json says accept-edited, and
    the terminal never re-prompts."""
    fake_code("edit")
    work = tmp_path / "work"
    work.mkdir()
    (work / "app.py").write_text("original\n", encoding="utf-8")
    answers = Answers(["v"])
    runner = make_runner(
        decider=_decider(answers, VSCodeDiffOpener()),
        inline_review=True)
    outcome = runner.process_text(
        "```file\npath: app.py\nop: update\nproposed\n```", str(work))
    assert outcome.state == RunState.COMPLETED
    assert answers.taken == ["v"]  # decided in the editor, no re-prompt
    assert (work / "app.py").read_text(encoding="utf-8") == \
        "proposed\nedited in VS Code\n"
    assert "✓ app.py — UPDATED (edited in review)" in outcome.result_block
    decisions = list((tmp_path / "state").rglob("decision.json"))
    assert len(decisions) == 1
    record = read_decision(decisions[0].parent)
    assert record is not None and record.decision == "accept-edited"


# -- close without saving: back to the prompt ------------------------------

def test_readonly_view_reprompts_and_accept_applies_original(
        tmp_path, fake_code):
    """Variant B: the editor exits without touching anything, so
    the decider re-prompts; the following accept applies the
    ORIGINAL staged bytes, unmodified."""
    fake_code("")  # read-only view
    work, candidate = _stage(
        tmp_path,
        [FileSpec(path="a.txt", declared_op="update", content="new\n")],
        {"a.txt": "old\n"})
    answers = Answers(["v", "a"])
    record = _decider(answers, VSCodeDiffOpener())(candidate)
    assert record is not None and record.decision == "accept"
    assert answers.taken == ["v", "a"]  # the view decided nothing
    write_decision(candidate, record)
    report = apply_candidate(candidate, record)
    assert report.ok
    assert (work / "a.txt").read_text(encoding="utf-8") == "new\n"
    assert report.entries[0].reviewer_modified is False


def test_noop_save_does_not_register_as_edit(tmp_path, fake_code):
    """A save that rewrites identical bytes (mtimes move, content
    doesn't) must not conclude the review — detection is by hash."""
    fake_code("noop")
    _work, candidate = _stage(
        tmp_path,
        [FileSpec(path="a.txt", declared_op="update", content="new\n")],
        {"a.txt": "old\n"})
    answers = Answers(["v", "a"])
    record = _decider(answers, VSCodeDiffOpener())(candidate)
    assert record is not None and record.decision == "accept"
    assert answers.taken == ["v", "a"]


# -- deletes are outside the save-detection set ----------------------------

def test_delete_target_touched_while_viewing_is_not_an_edit(
        tmp_path, fake_code):
    """Viewing a delete opens its LIVE file; even if that file
    changes during the view, the gesture must not fire — a delete
    has no staged content to edit. (The gate's stale-base check is
    what guards the apply later; the decider just re-prompts.)"""
    fake_code("touch-live")
    work, candidate = _stage(
        tmp_path,
        [FileSpec(path="a.txt", declared_op="update", content="new\n"),
         FileSpec(path="gone.txt", declared_op="delete")],
        {"a.txt": "old\n", "gone.txt": "doomed\n"})
    answers = Answers(["v", "a"])
    record = _decider(answers, VSCodeDiffOpener())(candidate)
    assert record is not None and record.decision == "accept"
    # Two answers to decide (view, accept) plus the multi-entry
    # prompt's files question, answered "" = all — the gesture
    # conclusively did NOT fire on the first answer.
    assert answers.taken == ["v", "a", ""]
    # Vacuity guard: the fake really did modify the delete's target.
    assert (work / "gone.txt").read_text(encoding="utf-8") == \
        "doomed\ntouched while viewing\n"


# -- failure posture --------------------------------------------------------

def test_broken_code_falls_back_and_reprompts(tmp_path, fake_code):
    """`code` erroring on every invocation (e.g. `--wait`
    unsupported) never crashes or hangs the review: the opener
    falls back to fire-and-forget, reports no edits, and the
    terminal prompt repeats."""
    fake_code("fail")
    work, candidate = _stage(
        tmp_path,
        [FileSpec(path="a.txt", declared_op="update", content="new\n")],
        {"a.txt": "old\n"})
    answers = Answers(["v", "a"])
    record = _decider(answers, VSCodeDiffOpener())(candidate)
    assert record is not None and record.decision == "accept"
    assert answers.taken == ["v", "a"]
    staged = candidate.dir / "candidates" / candidate.entries[0].candidate_file
    assert staged.read_bytes() == b"new\n"  # nothing snuck in


def test_missing_code_binary_reports_no_edits(tmp_path):
    """No `code` on PATH at all: the opener returns False instead
    of raising."""
    _work, candidate = _stage(
        tmp_path,
        [FileSpec(path="a.txt", declared_op="update", content="new\n")],
        {"a.txt": "old\n"})
    opener = VSCodeDiffOpener(code_cmd=str(tmp_path / "no-such-code"))
    assert opener(candidate) is False


# -- the opener seam itself -------------------------------------------------

def test_opener_return_contract(tmp_path, fake_code):
    """True exactly when a staged copy's content changed during the
    waiting session; the manifest summary is written either way."""
    _work, candidate = _stage(
        tmp_path,
        [FileSpec(path="a.txt", declared_op="update", content="new\n")],
        {"a.txt": "old\n"})
    fake_code("edit")
    assert VSCodeDiffOpener()(candidate) is True
    assert (candidate.dir / "summary.txt").exists()

    _work2, candidate2 = _stage(
        tmp_path / "second",
        [FileSpec(path="a.txt", declared_op="update", content="new\n")],
        {"a.txt": "old\n"})
    fake_code("")
    assert VSCodeDiffOpener()(candidate2) is False
