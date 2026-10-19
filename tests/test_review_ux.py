"""Review-UX rules: wherever a human is in the loop the TERMINAL is
the default review surface — VS Code opens only when the human picks
it inside the prompt — and a human-fired trigger decides a staged
candidate in the same invocation (deferring parks it as usual). The
passive watch never prompts and never opens an editor.
"""

from __future__ import annotations

import types

from clip_harness import cli
from clip_harness.adapters.headless import (MemoryClipboard,
                                            StaticDecider,
                                            TerminalDecider)
from clip_harness.core import FileSpec, RunState
from clip_harness.gate import DecisionRecord, classify_and_stage
from clip_harness.settings import Settings

FILE_PAYLOAD = ("```file\npath: src/new.py\nop: create\n"
                "print('new')\n```")
FILE_PAYLOAD_ATTENDED = ("```shell\nattend: true\n```\n" + FILE_PAYLOAD)


# -- trigger context: same roundtrip ------------------------------------

def test_trigger_context_reviews_pure_file_payload_inline(
        make_runner, tmp_path):
    """A pure file payload (no shell header — it cannot express
    `attend:`) is decided in the SAME invocation when the runner was
    wired for inline review, as the trigger / `run` entries wire it."""
    work = tmp_path / "work"
    work.mkdir()
    runner = make_runner(
        decider=StaticDecider(DecisionRecord(decision="accept")),
        inline_review=True)
    outcome = runner.process_text(FILE_PAYLOAD, str(work))
    assert outcome.state == RunState.COMPLETED
    assert (work / "src" / "new.py").read_text() == "print('new')\n"
    assert "✓ src/new.py — NEW" in outcome.result_block
    statuses = [e["data"].get("status")
                for e in runner.ledger.entries(run_id=outcome.run_id)
                if e["event"] == "ReviewDecision"]
    assert "applied" in statuses and "pending" not in statuses


def test_trigger_context_defer_parks_and_later_review_completes(
        make_runner, tmp_path, clipboard):
    """Decider returns None (the human picked 'later'): the run parks
    exactly as an async run — pending note on the clipboard, pending
    ReviewDecision recorded, nothing applied — and a later review
    completes it."""
    work = tmp_path / "work"
    work.mkdir()
    runner = make_runner(decider=StaticDecider(None), inline_review=True)
    outcome = runner.process_text(FILE_PAYLOAD, str(work))
    assert outcome.state == RunState.PENDING_REVIEW
    assert "awaiting review" in clipboard.text
    assert not (work / "src" / "new.py").exists()
    statuses = [e["data"].get("status")
                for e in runner.ledger.entries(run_id=outcome.run_id)
                if e["event"] == "ReviewDecision"]
    assert "pending" in statuses
    done = runner.review_candidate(
        outcome.run_id, StaticDecider(DecisionRecord(decision="accept")))
    assert done.state == RunState.COMPLETED
    assert (work / "src" / "new.py").read_text() == "print('new')\n"


def test_watch_style_runner_parks_even_attended_payload(
        make_runner, tmp_path):
    """The watch wiring carries no decider at all: even an
    `attend: true` payload parks — nothing is consulted, nothing is
    presented inline, no editor opens."""
    work = tmp_path / "work"
    work.mkdir()
    runner = make_runner(decider=None)  # as cmd_watch builds it
    outcome = runner.process_text(FILE_PAYLOAD_ATTENDED, str(work))
    assert outcome.state == RunState.PENDING_REVIEW
    assert not (work / "src" / "new.py").exists()


# -- CLI wiring ----------------------------------------------------------

class _StubRunner:
    def __init__(self):
        self.process_calls = []

    def process_text(self, text, cwd, allow_bare=False):
        self.process_calls.append(text)
        return None

    def arm(self, cwd):
        pass

    def session_end(self, cwd):
        return {}


def _capture_cli_runner(monkeypatch, tmp_path):
    captured: dict = {}
    stub = _StubRunner()

    def factory(**kwargs):
        captured.update(kwargs)
        return stub

    monkeypatch.setattr(cli, "LifecycleRunner", factory)
    monkeypatch.setattr(
        cli, "load_settings",
        lambda: Settings(state_dir=tmp_path / "state"))
    monkeypatch.setattr(
        cli, "_default_clipboard",
        lambda: MemoryClipboard("```shell\necho hi\n```"))
    return captured, stub


def test_cli_trigger_wires_inline_review(monkeypatch, tmp_path):
    captured, _stub = _capture_cli_runner(monkeypatch, tmp_path)
    assert cli.cmd_trigger(types.SimpleNamespace()) == 0
    assert captured.get("inline_review") is True
    assert captured.get("decider") is not None


def test_cli_run_wires_inline_review(monkeypatch, tmp_path):
    captured, _stub = _capture_cli_runner(monkeypatch, tmp_path)
    args = cli.build_parser().parse_args(["run", "--command", "echo hi"])
    cli.cmd_run(args)
    assert captured.get("inline_review") is True


def test_cli_watch_wires_no_decider_no_opener(monkeypatch, tmp_path):
    import clip_harness.adapters.watch_local as watch_local

    class _StubWatch:
        def __init__(self, runner, clipboard, cwd):
            pass

        def run_forever(self):
            return None

    monkeypatch.setattr(watch_local, "WatchLoop", _StubWatch)
    captured, _stub = _capture_cli_runner(monkeypatch, tmp_path)
    args = cli.build_parser().parse_args(["watch"])
    assert cli.cmd_watch(args) == 0
    assert captured.get("decider") is None
    assert captured.get("diff_opener") is None
    assert not captured.get("inline_review")


# -- TerminalDecider: VS Code is an in-prompt option ---------------------

def _staged_candidate(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    specs = [FileSpec(path="a.txt", declared_op="create",
                      content="hello\n")]
    return classify_and_stage(tmp_path / "cand", "r-test", None,
                              specs, work)


def _scripted_decider(answers, opener=None, prompts=None):
    it = iter(answers)

    def _input(prompt=""):
        if prompts is not None:
            prompts.append(prompt)
        return next(it)

    return TerminalDecider(input_fn=_input, out=lambda _s: None,
                           diff_opener=opener)


def test_terminal_decider_opens_vscode_only_on_request(tmp_path):
    candidate = _staged_candidate(tmp_path)
    opened = []
    decider = _scripted_decider(["v", "a"],
                                 opener=lambda c: opened.append(c))
    record = decider(candidate)
    assert record is not None and record.decision == "accept"
    assert opened == [candidate]  # exactly once, then re-prompted


def test_terminal_decider_no_view_no_open(tmp_path):
    candidate = _staged_candidate(tmp_path)
    opened = []
    decider = _scripted_decider(["a"],
                                 opener=lambda c: opened.append(c))
    record = decider(candidate)
    assert record is not None and record.decision == "accept"
    assert opened == []


def test_terminal_decider_view_then_defer(tmp_path):
    candidate = _staged_candidate(tmp_path)
    opened = []
    decider = _scripted_decider(["v", ""],
                                 opener=lambda c: opened.append(c))
    assert decider(candidate) is None  # viewing is not a decision
    assert opened == [candidate]


def test_terminal_decider_prompt_advertises_vscode_only_when_wired(
        tmp_path):
    candidate = _staged_candidate(tmp_path)
    prompts: list[str] = []
    _scripted_decider(["a"], prompts=prompts)(candidate)
    assert all("VS Code" not in p for p in prompts)
    prompts = []
    _scripted_decider(["a"], opener=lambda c: None,
                      prompts=prompts)(candidate)
    assert any("VS Code" in p for p in prompts)
