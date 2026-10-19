"""Contract tests: the hook schema and the exit-code/decision matrix.

The contract is enforced in code (clip_harness.contract) because the
whole point of moving off shell was that contracts must be checkable.
"""

import json
import subprocess

import pytest

from clip_harness.contract import validate_hook_input, validate_hook_output
from clip_harness.core import Event
from clip_harness.hooks import build_hook_input, dispatch
from clip_harness.settings import HookHandler, Settings


def valid_input(event=Event.PRE_RUN, **overrides):
    data = build_hook_input(
        event, session_id="s1", run_id="r-1", cwd="/tmp",
        clipboard_sha256="ab12", tool_name="Bash",
        tool_input={"command": "ls"}, tool_use_id="r-1")
    data.update(overrides)
    return data


# -- input schema -----------------------------------------------------------

def test_valid_hook_input_passes():
    assert validate_hook_input(valid_input()) == []


def test_hook_input_missing_required_field():
    data = valid_input()
    del data["tool_name"]
    errors = validate_hook_input(data)
    assert any("tool_name" in e for e in errors)


def test_hook_input_bad_event_and_types():
    errors = validate_hook_input(valid_input(hook_event_name="Nope"))
    assert any("hook_event_name" in e for e in errors)
    errors = validate_hook_input(valid_input(duration_ms="soon",
                                            tool_response=None))
    assert any("duration_ms" in e for e in errors)


def test_hook_input_not_an_object():
    assert validate_hook_input([1, 2]) != []


# -- output schema ------------------------------------------------------------

def test_empty_output_is_valid():
    assert validate_hook_output({}) == []


def test_valid_decision_output():
    out = {"hookSpecificOutput": {
        "hookEventName": "PreRun", "permissionDecision": "deny",
        "permissionDecisionReason": "no"}}
    assert validate_hook_output(out) == []


def test_decision_requires_event_name():
    out = {"hookSpecificOutput": {"permissionDecision": "deny"}}
    assert validate_hook_output(out) != []


def test_bad_decision_enum_is_a_violation():
    out = {"hookSpecificOutput": {
        "hookEventName": "PreRun", "permissionDecision": "maybe"}}
    assert validate_hook_output(out) != []


def test_registered_tools_shape_is_validated():
    good = {"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "registeredTools": [{"name": "x", "description": "y"}]}}
    assert validate_hook_output(good) == []
    bad = {"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "registeredTools": [{"name": 3}]}}
    assert validate_hook_output(bad) != []


def test_unknown_keys_are_tolerated():
    assert validate_hook_output({"futureField": 1}) == []


# -- exit-code / decision matrix through dispatch ------------------------------

class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _settings(event_name="PreRun"):
    return Settings(hooks={event_name: [HookHandler(command="hook")]})


def _run(returncode=0, stdout="", stderr="", event=Event.PRE_RUN,
         settings=None, hook_input=None):
    def fake_runner(command, **kwargs):
        return _Proc(returncode, stdout, stderr)
    return dispatch(event, hook_input or valid_input(event),
                    settings or _settings(event.value),
                    runner=fake_runner)


def _decision_json(decision, **extra):
    specific = {"hookEventName": "PreRun",
                "permissionDecision": decision}
    specific.update(extra)
    return json.dumps({"hookSpecificOutput": specific})


def test_exit0_no_output_means_no_opinion():
    outcome = _run(0, "")
    assert outcome.decision is None and not outcome.error
    assert outcome.gate_failed(Event.PRE_RUN) is False


def test_exit0_json_deny():
    outcome = _run(0, _decision_json("deny",
                                     permissionDecisionReason="bad"))
    assert outcome.decision == "deny" and outcome.reason == "bad"


def test_exit0_json_ask():
    outcome = _run(0, _decision_json("ask"))
    assert outcome.decision == "ask"


def test_exit0_updated_input():
    outcome = _run(0, _decision_json(
        "allow", updatedInput={"command": "ls -la"}))
    assert outcome.updated_input == {"command": "ls -la"}


def test_exit2_hard_block_wins_over_json_allow():
    outcome = _run(2, _decision_json("allow"), stderr="stop right there")
    assert outcome.hard_block is True
    assert outcome.hard_block_reason == "stop right there"
    assert outcome.gate_failed(Event.PRE_RUN) is True


def test_exit3_is_a_hook_error_and_gate_fails_closed():
    outcome = _run(3, "", stderr="boom")
    assert outcome.error
    assert outcome.gate_failed(Event.PRE_RUN) is True
    # ...but the same error on an observational event fails open.
    assert outcome.gate_failed(Event.POST_RUN) is False


def test_unparseable_output_is_a_hook_error():
    outcome = _run(0, "not json at all")
    assert outcome.error
    assert outcome.gate_failed(Event.PRE_RUN) is True


def test_non_object_output_is_a_hook_error():
    outcome = _run(0, "[1, 2]")
    assert outcome.error


def test_hook_timeout_is_a_hook_error():
    def timing_out(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 5)
    outcome = dispatch(Event.PRE_RUN, valid_input(), _settings(),
                       runner=timing_out)
    assert outcome.error and "timed out" in outcome.error
    assert outcome.gate_failed(Event.PRE_RUN) is True


def test_schema_violation_on_gate_fails_closed():
    outcome = _run(0, json.dumps({"hookSpecificOutput": {
        "permissionDecision": "deny"}}))  # missing hookEventName
    assert outcome.error and "schema" in outcome.error
    assert outcome.decision is None
    assert outcome.gate_failed(Event.PRE_RUN) is True


def test_schema_violation_on_observational_is_ignored(tmp_path):
    # A bad PostRun output must not stop a later good handler.
    settings = Settings(hooks={"PostRun": [HookHandler(command="bad"),
                                           HookHandler(command="good")]})
    outputs = iter([
        _Proc(0, json.dumps({"hookSpecificOutput": {
            "permissionDecision": "deny"}})),
        _Proc(0, json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostRun",
            "additionalContext": "from the good hook"}})),
    ])

    def fake_runner(command, **kwargs):
        return next(outputs)

    outcome = dispatch(Event.POST_RUN,
                       valid_input(Event.POST_RUN,
                                   tool_response={"output": "x"},
                                   error=None, duration_ms=3),
                       settings, runner=fake_runner)
    assert outcome.additional_context == ["from the good hook"]
    assert outcome.gate_failed(Event.POST_RUN) is False


def test_deny_beats_ask_beats_allow():
    settings = Settings(hooks={"PreRun": [HookHandler(command="a"),
                                          HookHandler(command="b"),
                                          HookHandler(command="c")]})
    outputs = iter([_Proc(0, _decision_json("allow")),
                    _Proc(0, _decision_json("ask")),
                    _Proc(0, _decision_json("deny"))])
    outcome = dispatch(Event.PRE_RUN, valid_input(), settings,
                       runner=lambda command, **kw: next(outputs))
    assert outcome.decision == "deny"


def test_input_schema_violation_never_reaches_handlers():
    calls = []

    def fake_runner(command, **kwargs):
        calls.append(command)
        return _Proc(0, "")

    outcome = dispatch(Event.PRE_RUN, {"bogus": True}, _settings(),
                       runner=fake_runner)
    assert outcome.error and "schema" in outcome.error
    assert calls == []


def test_matcher_and_if_pattern_filter_handlers():
    settings = Settings(hooks={"PreRun": [
        HookHandler(command="write-only", matcher="Write"),
        HookHandler(command="git-only", matcher="Bash",
                    if_pattern="git *"),
        HookHandler(command="all-bash", matcher="Bash"),
    ]})
    ran = []

    def fake_runner(command, **kwargs):
        ran.append(command)
        return _Proc(0, "")

    dispatch(Event.PRE_RUN, valid_input(), settings, runner=fake_runner)
    assert ran == ["all-bash"]  # 'ls' matches neither Write nor git *
