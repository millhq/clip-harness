"""Reviewed shell commands: the `writes:` header, command review,
command candidates, the Seatbelt sandbox, and always-allow.

A shell command whose gate verdict is ASK used to park with a note
telling the model to re-send as file/patch fences — no decision
path could ever approve it. These tests pin the real flow: stage as
a command candidate, decide (sandboxed / unsandboxed / always /
reject / later), execute on accept.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import FakeExecutor

from clip_harness import cli, sandbox
from clip_harness.adapters.headless import (MemoryClipboard, StaticDecider,
                                            TerminalCommandDecider)
from clip_harness.core import Decision, RunState
from clip_harness.executor import LocalExecutor
from clip_harness.gate import (DecisionRecord, evaluate_policy,
                               stage_command_candidate)
from clip_harness.payload import PayloadError, parse_clipboard_text, \
    parse_payload
from clip_harness.settings import Settings, load_settings

MUTATING = "mkdir -p dist && echo evidence > dist/out.txt"


def _work(tmp_path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    return work


def _review_statuses(runner, run_id) -> list:
    return [e["data"] for e in runner.ledger.entries(run_id=run_id)
            if e["event"] == "ReviewDecision"]


# -- parser: the writes: header ------------------------------------------

def test_writes_header_fenced():
    payload = parse_payload(
        "```shell\nwrites: dist, logs/out.txt\nmkdir -p dist\n```")
    assert payload.writes == ["dist", "logs/out.txt"]
    assert payload.command == "mkdir -p dist"


def test_writes_header_unfenced():
    payload = parse_clipboard_text(
        "session: s1\nwrites: dist/raw-evidence\nmake evidence")
    assert payload is not None
    assert payload.writes == ["dist/raw-evidence"]
    assert payload.command == "make evidence"


def test_writes_header_entries_trimmed():
    payload = parse_payload(
        "```shell\nwrites:  a , b/c ,  d  \ntrue\n```")
    assert payload.writes == ["a", "b/c", "d"]


@pytest.mark.parametrize("text", [
    "```shell\nwrites:\nmkdir -p dist\n```",          # empty, fenced
    "```shell\nwrites: , ,\nmkdir -p dist\n```",      # commas only
    "writes:\nmkdir -p dist",                          # empty, unfenced
])
def test_writes_empty_value_fails_closed(text):
    with pytest.raises(PayloadError):
        parse_clipboard_text(text, allow_bare=True)


def test_no_writes_header_means_no_declaration():
    payload = parse_payload("```shell\nls\n```")
    assert payload.writes == []


# -- settings: shell_sandbox ----------------------------------------------

def test_shell_sandbox_default_and_validation(tmp_path):
    work = _work(tmp_path)
    (work / ".clip-harness").mkdir()
    absent = tmp_path / "no-user.json"
    settings = load_settings(work, user_path=absent)
    assert settings.shell_sandbox == "optional"
    (work / ".clip-harness" / "settings.json").write_text(
        json.dumps({"shell_sandbox": "strict"}), encoding="utf-8")
    assert load_settings(work, user_path=absent).shell_sandbox == "strict"
    (work / ".clip-harness" / "settings.json").write_text(
        json.dumps({"shell_sandbox": "bogus"}), encoding="utf-8")
    assert load_settings(work, user_path=absent).shell_sandbox == "optional"


def test_shell_sandbox_is_part_of_config_hash():
    assert Settings(shell_sandbox="optional").config_hash() != \
        Settings(shell_sandbox="strict").config_hash()


# -- gate: allowlist approval semantics -----------------------------------

def test_allowlisted_mutation_allows():
    verdict = evaluate_policy("touch x", Settings(allowlist=["touch *"]))
    assert verdict.decision == Decision.ALLOW


def test_allowlist_miss_still_asks():
    verdict = evaluate_policy("touch x", Settings(allowlist=["git *"]))
    assert verdict.decision == Decision.ASK


def test_mutation_gate_deny_beats_allowlist():
    s = Settings(allowlist=["touch *"], mutation_gate="deny")
    assert evaluate_policy("touch x", s).decision == Decision.DENY


# -- sandbox profile generation -------------------------------------------

def test_profile_allows_scope_and_denies_by_default(tmp_path):
    work = _work(tmp_path)
    profile = sandbox.build_profile(
        cwd=work, writes=[work / "dist"], tmpdirs=[Path("/tmp")],
        config_dir=work / "cfg")
    assert "(version 1)" in profile
    assert "(allow default)" in profile
    # Writes denied by default BEFORE the scoped allows.
    assert profile.index("(deny file-write*)") \
        < profile.index("(allow file-write*")
    allow_block = profile.split("(allow file-write*")[1].split(")")[0]
    assert f'(subpath "{work}")' in profile
    assert f'(subpath "{work / "dist"}")' in profile
    assert '(subpath "/tmp")' in profile
    assert '(literal "/dev/null")' in profile


def test_profile_carveouts_come_last_and_win(tmp_path):
    work = _work(tmp_path)
    profile = sandbox.build_profile(
        cwd=work, writes=[], tmpdirs=[], config_dir=work / "cfg")
    allow_at = profile.index("(allow file-write*")
    deny_at = profile.rindex("(deny file-write*")
    assert deny_at > allow_at  # last matching rule wins in Seatbelt
    carve = profile[deny_at:]
    assert f'(subpath "{work}/.git/hooks")' in carve
    assert f'(literal "{work}/.git/config")' in carve
    assert f'(subpath "{work}/cfg")' in carve  # the config dir passed in
    assert f'(subpath "{work}/.clip-harness")' in carve


def test_profile_escapes_quotes_and_backslashes(tmp_path):
    weird = tmp_path / 'quo"te\\back'
    weird.mkdir()
    profile = sandbox.build_profile(cwd=weird, writes=[], tmpdirs=[],
                                    config_dir=weird / "cfg")
    assert 'quo\\"te\\\\back' in profile


def test_sandbox_exec_absent_on_this_box():
    # The Linux test box has no sandbox-exec: sandboxed accepts must
    # fail closed here (the executor tests below pin that).
    assert sandbox.sandbox_exec_available() is False


# -- executor: the sandbox wrap -------------------------------------------

SHIM = ("#!/bin/sh\n"
        "printf '%s\\n' \"$@\" > \"$SANDBOX_SHIM_LOG\"\n"
        "shift 2\n"
        "exec \"$@\"\n")


@pytest.fixture
def shim_path(tmp_path, monkeypatch):
    bin_dir = tmp_path / "shimbin"
    bin_dir.mkdir()
    shim = bin_dir / "sandbox-exec"
    shim.write_text(SHIM, encoding="utf-8")
    shim.chmod(0o755)
    log = tmp_path / "sandbox-argv.log"
    monkeypatch.setenv("SANDBOX_SHIM_LOG", str(log))
    monkeypatch.setenv(
        "PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    return log


def _run_executor(tmp_path, command, **kwargs):
    import threading
    return LocalExecutor().run(command, str(tmp_path), 10,
                               threading.Event(), tmp_path / "out.txt",
                               **kwargs)


def test_executor_sandbox_wraps_and_captures(tmp_path, shim_path):
    profile = sandbox.build_profile(cwd=tmp_path, writes=[])
    result = _run_executor(tmp_path, "echo wrapped-hello",
                           sandbox_profile=profile)
    assert result.exit_desc == "0"
    assert result.output == "wrapped-hello\n"
    log = shim_path.read_text(encoding="utf-8")
    assert log.startswith("-p\n")
    assert "(deny file-write*)" in log      # the profile reached the shim
    assert "-c\necho wrapped-hello" in log  # then the wrapped command


def test_executor_sandbox_streamed_composes(tmp_path, shim_path):
    chunks: list[str] = []
    result = _run_executor(tmp_path, "echo streamed-hello",
                           sink=chunks.append,
                           sandbox_profile="(version 1)\n(allow default)\n")
    assert result.output == "streamed-hello\n"
    assert "".join(chunks) == "streamed-hello\n"
    assert "(allow default)" in shim_path.read_text(encoding="utf-8")


def test_executor_sandbox_missing_fails_closed(tmp_path, monkeypatch):
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))  # no sandbox-exec anywhere
    marker = tmp_path / "must-not-exist.txt"
    result = _run_executor(tmp_path, f"touch {marker}",
                           sandbox_profile="(version 1)\n")
    assert result.failed
    assert "sandbox-exec unavailable" in result.exit_desc
    assert "NOT run" in result.output
    assert not marker.exists()  # the command never executed


# -- runner: the review branch --------------------------------------------

def test_ask_bare_shell_accept_sandboxed_executes(
        make_runner, tmp_path):
    work = _work(tmp_path)
    executor = FakeExecutor(output="ran\n")
    runner = make_runner(
        decider=StaticDecider(DecisionRecord(decision="accept")),
        executor=executor, inline_review=True)
    payload = (f"```shell\nsession: nav\nwrites: dist\n{MUTATING}\n```")
    outcome = runner.process_text(payload, str(work))
    assert outcome.state == RunState.COMPLETED
    assert executor.calls == [MUTATING]
    profile = executor.kwargs[0].get("sandbox_profile")
    assert profile is not None
    assert f'(subpath "{work}")' in profile
    assert f'(subpath "{work / "dist"}")' in profile  # declared scope
    assert outcome.result_block.splitlines()[1] == \
        f"$ {MUTATING} (reviewed · sandboxed)"
    data = _review_statuses(runner, outcome.run_id)
    assert [d["status"] for d in data] == ["staged", "approved"]
    assert data[1]["mode"] == "sandboxed"
    assert data[1]["kind"] == "command"


def test_ask_bare_shell_reject_runs_nothing_reason_returned(
        make_runner, tmp_path, clipboard):
    work = _work(tmp_path)
    executor = FakeExecutor()
    runner = make_runner(
        decider=StaticDecider(DecisionRecord(
            decision="reject", reason="evidence goes elsewhere")),
        executor=executor, inline_review=True)
    outcome = runner.process_text(f"```shell\n{MUTATING}\n```", str(work))
    assert outcome.state == RunState.REJECTED
    assert executor.calls == []
    assert "command rejected — evidence goes elsewhere" in clipboard.text
    assert [e for e in runner.ledger.entries(run_id=outcome.run_id)
            if e["event"] == "PostRun"] == []
    data = _review_statuses(runner, outcome.run_id)
    assert data[-1]["status"] == "rejected"
    assert data[-1]["mode"] is None


def test_attend_true_reaches_the_review(make_runner, tmp_path):
    """Regression: attend used to do nothing for a bare ASK shell —
    the run parked with no decision path at all."""
    work = _work(tmp_path)
    executor = FakeExecutor()
    runner = make_runner(
        decider=StaticDecider(DecisionRecord(
            decision="accept-unsandboxed")),
        executor=executor)  # no inline_review: attend alone must do it
    outcome = runner.process_text(
        f"```shell\nattend: true\n{MUTATING}\n```", str(work))
    assert outcome.state == RunState.COMPLETED
    assert executor.calls == [MUTATING]
    assert "sandbox_profile" not in executor.kwargs[0]
    assert "(reviewed · unsandboxed)" in outcome.result_block


def test_watch_style_parks_command_candidate(
        make_runner, tmp_path, clipboard):
    work = _work(tmp_path)
    executor = FakeExecutor()
    runner = make_runner(decider=None, executor=executor)  # watch wiring
    outcome = runner.process_text(
        f"```shell\nattend: true\nwrites: dist\n{MUTATING}\n```",
        str(work))
    assert outcome.state == RunState.PENDING_REVIEW
    assert executor.calls == []
    assert f"clip-harness candidate review {outcome.run_id}" \
        in clipboard.text
    manifest = json.loads((runner.state_dir / "candidates"
                           / outcome.run_id / "manifest.json")
                          .read_text(encoding="utf-8"))
    assert manifest["kind"] == "command"
    assert manifest["command"] == MUTATING
    assert manifest["writes"] == ["dist"]
    assert manifest["resolved_writes"] == [str((work / "dist").resolve())]
    assert "mutates files" in manifest["reason"]
    assert manifest["clipboard_sha256"]
    pending = runner.pending_candidates()
    assert [c.candidate_id for c in pending] == [outcome.run_id]
    assert "command:" in pending[0].summary()


def test_always_allow_persists_pattern_and_repeat_not_held(
        make_runner, tmp_path):
    work = _work(tmp_path)
    executor = FakeExecutor()
    settings = Settings(state_dir=tmp_path / "state")
    runner = make_runner(
        settings=settings,
        decider=StaticDecider(DecisionRecord(decision="always-allow")),
        executor=executor, inline_review=True)
    outcome = runner.process_text(f"```shell\n{MUTATING}\n```", str(work))
    assert outcome.state == RunState.COMPLETED
    project_settings = json.loads(
        (work / ".clip-harness" / "settings.json").read_text(
            encoding="utf-8"))
    assert project_settings["allowlist"] == ["mkdir *"]
    changes = [e for e in runner.ledger.entries()
               if e["event"] == "ConfigChange"]
    assert changes and changes[-1]["data"]["reason"] == "always-allow"
    assert changes[-1]["data"]["pattern"] == "mkdir *"
    # A fresh process (fresh settings load) does not hold the repeat.
    fresh = load_settings(work, user_path=tmp_path / "absent.json")
    fresh.state_dir = tmp_path / "state"
    executor2 = FakeExecutor()
    runner2 = make_runner(settings=fresh, decider=None,
                          executor=executor2)
    repeat = runner2.process_text(f"```shell\n{MUTATING}\n```", str(work))
    assert repeat.state == RunState.COMPLETED
    assert executor2.calls == [MUTATING]
    assert "reviewed" not in repeat.result_block
    assert _review_statuses(runner2, repeat.run_id) == []


def test_strict_setting_refuses_unsandboxed(make_runner, tmp_path):
    work = _work(tmp_path)
    executor = FakeExecutor()
    runner = make_runner(
        settings=Settings(state_dir=tmp_path / "state",
                          shell_sandbox="strict"),
        decider=StaticDecider(DecisionRecord(
            decision="accept-unsandboxed")),
        executor=executor, inline_review=True)
    outcome = runner.process_text(f"```shell\n{MUTATING}\n```", str(work))
    assert outcome.state == RunState.FAILED
    assert "strict" in outcome.result_block
    assert executor.calls == []


def test_mixed_payload_files_then_command_review(make_runner, tmp_path):
    work = _work(tmp_path)
    from clip_harness.gate import Candidate, CommandCandidate
    executor = FakeExecutor(output="after\n")
    seen: list[str] = []

    def decide(candidate):
        seen.append(type(candidate).__name__)
        return DecisionRecord(decision="accept")

    runner = make_runner(decider=decide, executor=executor,
                         inline_review=True)
    payload = (f"```shell\nsession: mix\n{MUTATING}\n```\n"
               "```file\npath: src/new.py\nop: create\nprint('new')\n```")
    outcome = runner.process_text(payload, str(work))
    assert outcome.state == RunState.COMPLETED
    assert seen == ["Candidate", "CommandCandidate"]  # files first
    assert (work / "src" / "new.py").read_text() == "print('new')\n"
    assert executor.calls == [MUTATING]
    assert "✓ src/new.py — NEW" in outcome.result_block
    assert "(reviewed · sandboxed)" in outcome.result_block


def test_mutation_gate_deny_stays_flat_denial(make_runner, tmp_path):
    work = _work(tmp_path)
    runner = make_runner(
        settings=Settings(state_dir=tmp_path / "state",
                          mutation_gate="deny"),
        decider=StaticDecider(DecisionRecord(decision="accept")),
        inline_review=True)
    outcome = runner.process_text(f"```shell\n{MUTATING}\n```", str(work))
    assert outcome.state == RunState.DENIED
    assert not (runner.state_dir / "candidates").exists() or \
        not list((runner.state_dir / "candidates").iterdir())


def test_hard_block_stays_unreviewable(make_runner, tmp_path):
    work = _work(tmp_path)
    runner = make_runner(
        settings=Settings(state_dir=tmp_path / "state",
                          hard_block_patterns=[r"rm\s+-rf"]),
        decider=StaticDecider(DecisionRecord(decision="accept")),
        inline_review=True)
    outcome = runner.process_text("```shell\nrm -rf /\n```", str(work))
    assert outcome.state == RunState.DENIED
    assert _review_statuses(runner, outcome.run_id) == []


def test_allowlist_ask_reviews_and_runs_unsandboxed(
        make_runner, tmp_path):
    work = _work(tmp_path)
    executor = FakeExecutor()
    runner = make_runner(
        settings=Settings(state_dir=tmp_path / "state",
                          allowlist=["git *"]),
        decider=StaticDecider(DecisionRecord(
            decision="accept-unsandboxed")),
        executor=executor, inline_review=True)
    outcome = runner.process_text("```shell\nmake evidence\n```",
                                  str(work))
    assert outcome.state == RunState.COMPLETED
    assert executor.calls == ["make evidence"]


# -- parked candidate completed out-of-band --------------------------------

def _parked_command(make_runner, tmp_path, command="echo done > made.txt"):
    work = _work(tmp_path)
    runner = make_runner(decider=None, executor=LocalExecutor())
    outcome = runner.process_text(f"```shell\n{command}\n```", str(work))
    assert outcome.state == RunState.PENDING_REVIEW
    return runner, work, outcome.run_id


def test_review_candidate_accept_executes(make_runner, tmp_path):
    runner, work, cid = _parked_command(make_runner, tmp_path)
    done = runner.review_candidate(
        cid, StaticDecider(DecisionRecord(decision="accept-unsandboxed")))
    assert done.state == RunState.COMPLETED
    assert (work / "made.txt").read_text() == "done\n"
    assert "(reviewed · unsandboxed)" in done.result_block


def test_review_candidate_defer_stays_pending(make_runner, tmp_path):
    runner, work, cid = _parked_command(make_runner, tmp_path)
    done = runner.review_candidate(cid, StaticDecider(None))
    assert done.state == RunState.PENDING_REVIEW


# -- TerminalCommandDecider -------------------------------------------------

def _staged(tmp_path, command=MUTATING):
    work = _work(tmp_path)
    return stage_command_candidate(
        tmp_path / "cand", "r-test", "sess", command=command,
        cwd=str(work), timeout=120, writes=["dist"],
        reason="shell command mutates files — review gate required",
        clipboard_sha256="abc")


def _scripted(answers, shell_sandbox="optional", prompts=None):
    it = iter(answers)

    def _input(prompt=""):
        if prompts is not None:
            prompts.append(prompt)
        return next(it)

    return TerminalCommandDecider(input_fn=_input, out=lambda _s: None,
                                  shell_sandbox=shell_sandbox)


def test_terminal_command_decider_vocabulary(tmp_path):
    candidate = _staged(tmp_path)
    assert _scripted(["a"])(candidate).decision == "accept"
    assert _scripted(["u"])(candidate).decision == "accept-unsandboxed"
    assert _scripted(["w"])(candidate).decision == "always-allow"
    record = _scripted(["r", "not here"])(candidate)
    assert record.decision == "reject" and record.reason == "not here"
    assert _scripted([""])(candidate) is None  # Enter defers


def test_terminal_command_decider_always_label_shows_pattern(tmp_path):
    candidate = _staged(tmp_path)
    prompts: list[str] = []
    _scripted(["a"], prompts=prompts)(candidate)
    assert any("al[w]ays allow 'mkdir *'" in p for p in prompts)


def test_terminal_command_decider_strict_hides_unsandboxed(tmp_path):
    candidate = _staged(tmp_path)
    prompts: list[str] = []
    record = _scripted(["u"], shell_sandbox="strict",
                       prompts=prompts)(candidate)
    assert record is None  # not offered; the key defers like any unknown
    assert all("unsandboxed" not in p for p in prompts)


def test_command_candidate_presentation_contents(tmp_path):
    candidate = _staged(tmp_path)
    text = candidate.presentation()
    assert MUTATING in text
    assert f"cwd: {candidate.cwd}" in text
    assert "session: sess" in text
    assert "timeout: 120s" in text
    assert "mutates files" in text
    assert str((Path(candidate.cwd) / "dist").resolve()) in text
    assert ".git/hooks" in text and ".clip-harness" in text


def test_command_candidate_presentation_undeclared_scope(tmp_path):
    work = _work(tmp_path)
    candidate = stage_command_candidate(
        tmp_path / "cand", "r-test", None, command="make x",
        cwd=str(work), timeout=60, writes=[], reason="not allowlisted",
        clipboard_sha256="")
    text = candidate.presentation()
    assert "not declared — writes confined to the working directory" \
        in text
    assert "session:" not in text


# -- candidate CLI ----------------------------------------------------------

def _cli_env(monkeypatch, tmp_path):
    settings = Settings(state_dir=tmp_path / "state")
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    clip = MemoryClipboard()
    monkeypatch.setattr(cli, "_default_clipboard", lambda: clip)
    return settings, clip


def test_cli_review_command_candidate_accept_sandboxed(
        make_runner, tmp_path, monkeypatch, shim_path, capsys):
    _settings, _clip = _cli_env(monkeypatch, tmp_path)
    runner, work, cid = _parked_command(
        make_runner, tmp_path, "echo via-shim > shim.txt")
    # _parked_command's runner shares the CLI's state dir? No — align:
    assert runner.state_dir == tmp_path / "state"
    rc = cli.main(["candidate", "review", cid, "--decision", "accept"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "(reviewed · sandboxed)" in out
    assert (work / "shim.txt").read_text() == "via-shim\n"
    assert "(deny file-write*)" in shim_path.read_text(encoding="utf-8")


def test_cli_review_command_candidate_accept_unsandboxed(
        make_runner, tmp_path, monkeypatch, capsys):
    _cli_env(monkeypatch, tmp_path)
    _runner, work, cid = _parked_command(
        make_runner, tmp_path, "echo bare > bare.txt")
    rc = cli.main(["candidate", "review", cid,
                   "--decision", "accept-unsandboxed"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "(reviewed · unsandboxed)" in out
    assert (work / "bare.txt").read_text() == "bare\n"


def test_cli_review_command_candidate_reject(
        make_runner, tmp_path, monkeypatch, capsys):
    _cli_env(monkeypatch, tmp_path)
    _runner, work, cid = _parked_command(
        make_runner, tmp_path, "echo no > no.txt")
    rc = cli.main(["candidate", "review", cid, "--decision", "reject",
                   "--reason", "not today"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "command rejected — not today" in out
    assert not (work / "no.txt").exists()


def test_cli_decide_then_apply_command_candidate(
        make_runner, tmp_path, monkeypatch, capsys):
    _cli_env(monkeypatch, tmp_path)
    _runner, work, cid = _parked_command(
        make_runner, tmp_path, "echo later > later.txt")
    rc = cli.main(["candidate", "decide", cid,
                   "--decision", "accept-unsandboxed"])
    assert rc == 0
    assert not (work / "later.txt").exists()  # decide only records
    rc = cli.main(["candidate", "apply", cid])
    assert rc == 0
    assert (work / "later.txt").read_text() == "later\n"


def test_cli_show_and_list_command_candidate(
        make_runner, tmp_path, monkeypatch, capsys):
    _cli_env(monkeypatch, tmp_path)
    _runner, _work_dir, cid = _parked_command(make_runner, tmp_path)
    rc = cli.main(["candidate", "list"])
    assert rc == 0
    assert cid in capsys.readouterr().out
    rc = cli.main(["candidate", "show", cid])
    assert rc == 0
    out = capsys.readouterr().out
    assert "command review" in out and "echo done > made.txt" in out


def test_cli_rejects_cross_kind_decision_vocabulary(
        make_runner, tmp_path, monkeypatch, capsys):
    _cli_env(monkeypatch, tmp_path)
    _runner, _work_dir, cid = _parked_command(make_runner, tmp_path)
    rc = cli.main(["candidate", "review", cid,
                   "--decision", "accept-edited"])
    assert rc == 1
    assert "accept-edited does not apply" in capsys.readouterr().err
