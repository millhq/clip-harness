"""Lifecycle tests: the full state machine through the runner, with
headless adapters only (memory clipboard, fake/local executors)."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

from clip_harness.adapters.headless import MemoryClipboard, StaticDecider
from clip_harness.core import Event, RunState
from clip_harness.executor import LocalExecutor
from clip_harness.gate import DecisionRecord
from clip_harness.goals import GoalStore
from clip_harness.settings import HookHandler, Settings

from conftest import FakeExecutor, shell_payload

ALL_EVENTS = ["SessionStart", "PayloadCaptured", "PreRun",
              "ReviewDecision", "PostRun", "PostRunFailure", "RunClosed",
              "SessionEnd", "ConfigChange"]


def recorder_settings(tmp_path, record_file: Path) -> Settings:
    script = tmp_path / "record.py"
    script.write_text(
        "import json, sys\n"
        "data = json.load(sys.stdin)\n"
        f"open({str(record_file)!r}, 'a').write("
        "data['hook_event_name'] + '\\n')\n")
    handler = HookHandler(command=f"{sys.executable} {script}",
                          matcher="*")
    return Settings(state_dir=tmp_path / "state",
                    hooks={name: [handler] for name in ALL_EVENTS})


def ledger_events(runner, run_id=None):
    return [(e["event"], e["data"].get("status"))
            for e in runner.ledger.entries(run_id=run_id)]


# -- happy path + ordering ----------------------------------------------------

def test_simple_run_event_order_and_receipts(make_runner, tmp_path):
    record = tmp_path / "events.txt"
    executor = FakeExecutor(output="hello\n")
    runner = make_runner(settings=recorder_settings(tmp_path, record),
                         executor=executor)
    outcome = runner.process_text(shell_payload("echo hello"), "/tmp")
    assert outcome.state == RunState.COMPLETED
    assert executor.calls == ["echo hello"]
    seen = record.read_text().splitlines()
    assert seen == ["SessionStart", "PayloadCaptured", "PreRun",
                    "PostRun", "RunClosed"]
    events = [name for name, _ in ledger_events(runner, outcome.run_id)]
    assert events == ["PayloadCaptured", "PostRun", "RunClosed"]
    # Result block format (inherited from the original loop).
    assert outcome.result_block.startswith("```result\n$ echo hello")
    assert "[exit 0 ·" in outcome.result_block


def test_receipt_written_before_clipboard_overwrite(make_runner, tmp_path):
    state = tmp_path / "state"
    ledger_path = state / "receipts.jsonl"
    observations = []

    class CheckingClipboard(MemoryClipboard):
        def set_text(self, text):
            if text.startswith("```result"):
                content = (ledger_path.read_text()
                           if ledger_path.exists() else "")
                observations.append('"PostRun"' in content)
            super().set_text(text)

    settings = Settings(state_dir=state)
    from clip_harness.runner import LifecycleRunner
    runner = LifecycleRunner(settings=settings,
                             clipboard=CheckingClipboard(),
                             presenter=lambda s: None,
                             executor=FakeExecutor())
    runner.process_text(shell_payload("echo hi"), "/tmp")
    assert observations == [True]


# -- session binding -------------------------------------------------------------

def test_unbound_run_still_runs(make_runner):
    executor = FakeExecutor()
    runner = make_runner(executor=executor)
    outcome = runner.process_text(shell_payload("echo free"), "/tmp")
    assert outcome.state == RunState.COMPLETED
    assert executor.calls == ["echo free"]
    assert "— goal:" not in outcome.result_block


def test_bound_run_registers_session(make_runner):
    runner = make_runner()
    runner.process_text(shell_payload("echo x", session="auth-1"), "/tmp")
    handle = runner.registry.get("auth-1")
    assert handle is not None and handle.binding_id == "auth-1"


def test_rerun_replays_stored_payload(make_runner):
    executor = FakeExecutor()
    runner = make_runner(executor=executor)
    runner.process_text(shell_payload("echo again", session="s9"), "/tmp")
    outcome = runner.process_text("```rerun id=s9```", "/tmp")
    assert outcome.state == RunState.COMPLETED
    assert executor.calls == ["echo again", "echo again"]


def test_cancel_trigger_with_no_run(make_runner):
    runner = make_runner()
    outcome = runner.process_text("```cancel id=ghost```", "/tmp")
    assert "no running run" in outcome.message


def test_cancel_running_run_via_event(make_runner, tmp_path):
    settings = Settings(state_dir=tmp_path / "state")
    from clip_harness.runner import LifecycleRunner
    runner = LifecycleRunner(settings=settings,
                             clipboard=MemoryClipboard(),
                             presenter=lambda s: None,
                             executor=LocalExecutor())
    result = {}

    def go():
        result["outcome"] = runner.process_text(
            shell_payload("sleep 10", session="nap"), str(tmp_path))

    thread = threading.Thread(target=go)
    thread.start()
    deadline = time.time() + 5
    handle = None
    while time.time() < deadline:
        handle = runner.registry.get("nap")
        if handle is not None and handle.state == "running":
            break
        time.sleep(0.05)
    assert handle is not None
    assert runner.registry.request_cancel("nap") is True
    thread.join(timeout=10)
    outcome = result["outcome"]
    assert outcome.state == RunState.CANCELLED
    statuses = dict(ledger_events(runner, outcome.run_id))
    assert statuses["PostRun"] == "cancelled"
    assert "PostRunFailure" not in statuses


# -- bare payloads ---------------------------------------------------------------

def test_bare_text_runs_on_trigger_path_only(make_runner):
    executor = FakeExecutor()
    runner = make_runner(executor=executor)
    outcome = runner.process_text('echo "hello world"', "/tmp",
                                  allow_bare=True)
    assert outcome.state == RunState.COMPLETED
    assert executor.calls == ['echo "hello world"']
    assert runner.process_text('echo "hello world"', "/tmp",
                               allow_bare=False) is None


def test_malformed_payload_is_receipted_and_denied(make_runner, clipboard):
    executor = FakeExecutor()
    runner = make_runner(executor=executor)
    outcome = runner.process_text("```shell\necho hi\n", "/tmp")
    assert outcome.state == RunState.DENIED
    assert executor.calls == []
    assert clipboard.text.startswith("⛔ clip-harness: malformed payload")
    entries = runner.ledger.entries(run_id=outcome.run_id)
    assert entries[0]["data"]["status"] == "rejected"


def test_oversized_payload_rejected_before_anything(make_runner):
    executor = FakeExecutor()
    runner = make_runner(executor=executor)
    big = "```shell\n" + ("echo " + "x" * 40_000) + "\n```"
    outcome = runner.process_text(big, "/tmp")
    assert outcome.state == RunState.DENIED
    assert executor.calls == []


# -- gate behavior through the runner ----------------------------------------------

def test_builtin_hard_block_denies(make_runner):
    executor = FakeExecutor()
    runner = make_runner(executor=executor)
    runner.settings.hard_block_patterns = [r"rm\s+-rf\s+/"]
    outcome = runner.process_text(shell_payload("rm -rf /"), "/tmp")
    assert outcome.state == RunState.DENIED
    assert executor.calls == []


def _hook_runner(make_runner, tmp_path, hook_script, script_body,
                 event="PreRun", executor=None):
    command = hook_script("gate.py", script_body)
    settings = Settings(state_dir=tmp_path / "state",
                        hooks={event: [HookHandler(command=command)]})
    return make_runner(settings=settings, executor=executor or FakeExecutor())


def test_gate_hook_crash_fails_closed(make_runner, hook_script, tmp_path):
    runner = _hook_runner(make_runner, tmp_path, hook_script,
                          "import sys; sys.exit(3)\n")
    outcome = runner.process_text(shell_payload("echo hi"), "/tmp")
    assert outcome.state == RunState.DENIED
    assert "failed closed" in outcome.message


def test_gate_hook_timeout_fails_closed(make_runner, hook_script, tmp_path):
    command = hook_script("slow.py", "import time; time.sleep(5)\n")
    settings = Settings(
        state_dir=tmp_path / "state",
        hooks={"PreRun": [HookHandler(command=command, timeout=0.2)]})
    executor = FakeExecutor()
    runner = make_runner(settings=settings, executor=executor)
    outcome = runner.process_text(shell_payload("echo hi"), "/tmp")
    assert outcome.state == RunState.DENIED
    assert executor.calls == []


def test_gate_hook_bad_json_fails_closed(make_runner, hook_script, tmp_path):
    runner = _hook_runner(make_runner, tmp_path, hook_script,
                          "print('definitely not json')\n")
    outcome = runner.process_text(shell_payload("echo hi"), "/tmp")
    assert outcome.state == RunState.DENIED


def test_gate_hook_exit2_blocks_with_stderr_reason(
        make_runner, hook_script, tmp_path, clipboard):
    runner = _hook_runner(
        make_runner, tmp_path, hook_script,
        "import sys; print('never on my watch', file=sys.stderr); "
        "sys.exit(2)\n")
    outcome = runner.process_text(shell_payload("echo hi"), "/tmp")
    assert outcome.state == RunState.DENIED
    assert "never on my watch" in clipboard.text


def test_gate_hook_deny_decision(make_runner, hook_script, tmp_path):
    body = ("import json\nprint(json.dumps({'hookSpecificOutput': {"
            "'hookEventName': 'PreRun', 'permissionDecision': 'deny', "
            "'permissionDecisionReason': 'not today'}}))\n")
    runner = _hook_runner(make_runner, tmp_path, hook_script, body)
    outcome = runner.process_text(shell_payload("echo hi"), "/tmp")
    assert outcome.state == RunState.DENIED
    assert "not today" in outcome.message


def test_gate_hook_ask_without_files_parks_pending(
        make_runner, hook_script, tmp_path):
    body = ("import json\nprint(json.dumps({'hookSpecificOutput': {"
            "'hookEventName': 'PreRun', 'permissionDecision': 'ask', "
            "'permissionDecisionReason': 'a human should see this'}}))\n")
    executor = FakeExecutor()
    runner = _hook_runner(make_runner, tmp_path, hook_script, body,
                          executor=executor)
    outcome = runner.process_text(shell_payload("echo hi"), "/tmp")
    assert outcome.state == RunState.PENDING_REVIEW
    assert executor.calls == []


def _hook_printing(payload: dict) -> str:
    return f"import json\nprint({json.dumps(payload)!r})\n"


def test_updated_input_rewrite_runs_rewritten_command(
        make_runner, hook_script, tmp_path):
    body = _hook_printing({"hookSpecificOutput": {
        "hookEventName": "PreRun", "permissionDecision": "allow",
        "updatedInput": {"command": "echo rewritten"}}})
    executor = FakeExecutor()
    runner = _hook_runner(make_runner, tmp_path, hook_script, body,
                          executor=executor)
    outcome = runner.process_text(shell_payload("echo original"), "/tmp")
    assert outcome.state == RunState.COMPLETED
    assert executor.calls == ["echo rewritten"]
    assert "$ echo rewritten" in outcome.result_block


def test_updated_input_rewrite_is_rechecked_against_policy(
        make_runner, hook_script, tmp_path):
    body = _hook_printing({"hookSpecificOutput": {
        "hookEventName": "PreRun", "permissionDecision": "allow",
        "updatedInput": {"command": "rm -rf /"}}})
    executor = FakeExecutor()
    runner = _hook_runner(make_runner, tmp_path, hook_script, body,
                          executor=executor)
    runner.settings.hard_block_patterns = [r"rm\s+-rf\s+/"]
    outcome = runner.process_text(shell_payload("echo harmless"), "/tmp")
    assert outcome.state == RunState.DENIED
    assert executor.calls == []


def test_denial_fires_no_postrun_hook(make_runner, tmp_path):
    record = tmp_path / "events.txt"
    settings = recorder_settings(tmp_path, record)
    settings.hard_block_patterns = [r"rm\s+-rf\s+/"]
    runner = make_runner(settings=settings, executor=FakeExecutor())
    outcome = runner.process_text(shell_payload("rm -rf /"), "/tmp")
    assert outcome.state == RunState.DENIED
    seen = record.read_text().splitlines() if record.exists() else []
    assert "PostRun" not in seen and "PostRunFailure" not in seen


# -- PostRunFailure separation ------------------------------------------------------

def test_failure_is_a_separate_event(make_runner, tmp_path):
    record = tmp_path / "events.txt"
    executor = FakeExecutor(output="boom\n", exit_code=3)
    runner = make_runner(settings=recorder_settings(tmp_path, record),
                         executor=executor)
    outcome = runner.process_text(shell_payload("make build"), "/tmp")
    assert outcome.state == RunState.FAILED
    events = [name for name, _ in ledger_events(runner, outcome.run_id)]
    assert "PostRunFailure" in events and "PostRun" not in events
    seen = record.read_text().splitlines()
    assert "PostRunFailure" in seen and "PostRun" not in seen


def test_postrun_hook_can_rewrite_clipboard_output_only(
        make_runner, hook_script, tmp_path):
    command = hook_script(
        "rewrite.py",
        "import json\nprint(json.dumps({'hookSpecificOutput': {"
        "'hookEventName': 'PostRun', "
        "'updatedToolOutput': 'summarised output'}}))\n")
    settings = Settings(state_dir=tmp_path / "state",
                        hooks={"PostRun": [HookHandler(command=command)]})
    runner = make_runner(settings=settings,
                         executor=FakeExecutor(output="raw output\n"))
    outcome = runner.process_text(shell_payload("echo x"), "/tmp")
    assert "summarised output" in outcome.result_block
    entries = runner.ledger.entries(run_id=outcome.run_id)
    post = [e for e in entries if e["event"] == "PostRun"][0]
    # The receipt keeps the TRUE output; only the clipboard is rewritten.
    assert post["data"]["tool_response"]["output"] == "raw output\n"
    assert post["data"]["output_rewritten_by_hook"] is True


# -- the review branch ------------------------------------------------------------------

FILE_PAYLOAD = ("```file\npath: src/new.py\nop: create\n"
                "print('new')\n```")
FILE_PAYLOAD_BOUND = ("```shell\nsession: rev-1\necho after-apply\n```\n"
                      + FILE_PAYLOAD)


def test_no_decision_leaves_run_pending_and_close_refused(
        make_runner, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    runner = make_runner(decider=None)  # nobody decides
    outcome = runner.process_text(FILE_PAYLOAD_BOUND, str(work))
    assert outcome.state == RunState.PENDING_REVIEW
    candidate_dir = (runner.state_dir / "candidates" / outcome.run_id)
    assert (candidate_dir / "manifest.json").exists()
    assert not (work / "src" / "new.py").exists()  # nothing applied
    # RunClosed refuses a clean close while the candidate pends.
    assert runner.close_run(outcome.run_id) is False
    entries = runner.ledger.entries(run_id=outcome.run_id)
    closed = [e for e in entries if e["event"] == "RunClosed"]
    assert closed and closed[-1]["data"]["status"] == "deferred"


def test_pending_candidate_completed_by_later_review(
        make_runner, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    executor = FakeExecutor(output="after\n")
    runner = make_runner(decider=None, executor=executor)
    outcome = runner.process_text(FILE_PAYLOAD_BOUND, str(work))
    assert outcome.state == RunState.PENDING_REVIEW
    assert executor.calls == []  # shell part waits for the review too
    done = runner.review_candidate(
        outcome.run_id,
        StaticDecider(DecisionRecord(decision="accept")))
    assert done.state == RunState.COMPLETED
    assert (work / "src" / "new.py").read_text() == "print('new')\n"
    assert "NEW" in done.result_block
    assert executor.calls == ["echo after-apply"]
    events = [n for n, _ in ledger_events(runner, outcome.run_id)]
    assert "PostRun" in events


def test_accept_applies_and_reports_new_tag(make_runner, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    runner = make_runner(
        decider=StaticDecider(DecisionRecord(decision="accept")))
    outcome = runner.process_text(FILE_PAYLOAD, str(work))
    assert outcome.state == RunState.COMPLETED
    assert (work / "src" / "new.py").read_text() == "print('new')\n"
    assert "✓ src/new.py — NEW" in outcome.result_block


def test_rejection_fires_no_postrun_and_writes_rejection_text(
        make_runner, tmp_path, clipboard):
    record = tmp_path / "events.txt"
    settings = recorder_settings(tmp_path, record)
    decider = StaticDecider(DecisionRecord(
        decision="reject", reason="wrong layer"))
    from clip_harness.runner import LifecycleRunner
    runner = LifecycleRunner(settings=settings, clipboard=clipboard,
                             decider=decider, presenter=lambda s: None,
                             executor=FakeExecutor())
    work = tmp_path / "work"
    work.mkdir()
    outcome = runner.process_text(FILE_PAYLOAD, str(work))
    assert outcome.state == RunState.REJECTED
    assert not (work / "src" / "new.py").exists()
    assert "changes rejected — wrong layer" in clipboard.text
    seen = record.read_text().splitlines()
    assert "PostRun" not in seen and "PostRunFailure" not in seen
    events = [n for n, _ in ledger_events(runner, outcome.run_id)]
    assert "PostRun" not in events and "PostRunFailure" not in events


def test_manifest_summary_is_presented_first(make_runner, tmp_path):
    presented = []
    work = tmp_path / "work"
    work.mkdir()
    from clip_harness.runner import LifecycleRunner
    runner = LifecycleRunner(
        settings=Settings(state_dir=tmp_path / "state"),
        clipboard=MemoryClipboard(),
        decider=StaticDecider(DecisionRecord(decision="accept")),
        presenter=presented.append, executor=FakeExecutor())
    runner.process_text(FILE_PAYLOAD, str(work))
    assert presented and "src/new.py · NEW" in presented[0]


def test_shell_plus_files_reviews_then_runs(make_runner, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    executor = FakeExecutor(output="tested\n")
    runner = make_runner(
        decider=StaticDecider(DecisionRecord(decision="accept")),
        executor=executor)
    text = ("```shell\nmake test\n```\n"
            "```file\npath: a.txt\nhello\n```")
    outcome = runner.process_text(text, str(work))
    assert outcome.state == RunState.COMPLETED
    assert (work / "a.txt").read_text() == "hello\n"
    assert executor.calls == ["make test"]
    assert "✓ a.txt — NEW" in outcome.result_block


# -- goal/task nag (core, zero hooks) --------------------------------------------

def test_bound_result_carries_goal_section_with_zero_hooks(make_runner):
    runner = make_runner()  # no hooks configured at all
    store = GoalStore(runner.state_dir, "auth-1")
    store.set_goal("Ship the auth refactor")
    store.add_task("wire tokens")
    store.add_task("update tests")
    store.set_task_status("wire tokens", "doing")
    store.add_task("old task")
    store.set_task_status("old task", "done")
    outcome = runner.process_text(
        shell_payload("echo go", session="auth-1"), "/tmp")
    assert "— goal: Ship the auth refactor [active]" in \
        outcome.result_block
    assert "[doing] wire tokens" in outcome.result_block
    assert "[open] update tests" in outcome.result_block
    assert "old task" not in outcome.result_block  # done tasks drop off


def test_goal_update_changes_next_result_block(make_runner):
    runner = make_runner()
    first = runner.process_text(
        shell_payload("echo 1", session="g1"), "/tmp")
    assert "— goal: (none set)" in first.result_block
    op = runner.process_text(
        "```shell\nsession: g1\naction: goal-set\n"
        "Migrate the billing jobs\n```", "/tmp")
    assert op.state == RunState.COMPLETED
    assert "goal set: Migrate the billing jobs" in op.result_block
    second = runner.process_text(
        shell_payload("echo 2", session="g1"), "/tmp")
    assert "— goal: Migrate the billing jobs [active]" \
        in second.result_block


def test_task_action_ops_round_trip(make_runner):
    runner = make_runner()
    runner.process_text(
        "```shell\nsession: t1\naction: task-add\n"
        "first task\nsecond task\n```", "/tmp")
    op = runner.process_text(
        "```shell\nsession: t1\naction: task-done\nfirst task\n```",
        "/tmp")
    assert "task done: first task" in op.result_block
    assert "[open] second task" in op.result_block
    assert "first task" not in op.result_block.split("open tasks:")[1]


def test_action_op_without_session_is_denied(make_runner):
    runner = make_runner()
    outcome = runner.process_text(
        "```shell\naction: goal-set\norphan goal\n```", "/tmp")
    assert outcome.state == RunState.DENIED


# -- capability manifest -----------------------------------------------------------

def test_settings_declared_tool_in_manifest(make_runner, tmp_path):
    settings = Settings(
        state_dir=tmp_path / "state",
        tools=[{"name": "demo.tool", "description": "A demo tool",
                "shape": "```demo fence"}])
    runner = make_runner(settings=settings)
    outcome = runner.process_text(
        shell_payload("echo 1", session="m1"), "/tmp")
    assert "— capabilities:" in outcome.result_block
    assert "demo.tool" in outcome.result_block
    # Refreshed once per change: the next block does not repeat it.
    second = runner.process_text(
        shell_payload("echo 2", session="m1"), "/tmp")
    assert "— capabilities:" not in second.result_block


def test_hook_registered_tool_appears_in_manifest(
        make_runner, hook_script, tmp_path):
    command = hook_script(
        "register.py",
        "import json\nprint(json.dumps({'hookSpecificOutput': {"
        "'hookEventName': 'SessionStart', 'registeredTools': [{"
        "'name': 'deploy.run', 'description': 'Deploy the service', "
        "'shape': '```deploy fence'}]}}))\n")
    settings = Settings(
        state_dir=tmp_path / "state",
        hooks={"SessionStart": [HookHandler(command=command)]})
    runner = make_runner(settings=settings)
    outcome = runner.process_text(
        shell_payload("echo 1", session="cap-1"), "/tmp")
    assert "deploy.run" in outcome.result_block


def test_tool_registered_mid_session_refreshes_next_block(
        make_runner, hook_script, tmp_path):
    command = hook_script(
        "register2.py",
        "import json, sys\n"
        "data = json.load(sys.stdin)\n"
        "cmd = (data.get('tool_input') or {}).get('command', '')\n"
        "if 'learn' in cmd:\n"
        "    print(json.dumps({'hookSpecificOutput': {"
        "'hookEventName': 'PreRun', 'registeredTools': [{"
        "'name': 'mid.tool', 'description': 'Learned mid-session', "
        "'shape': '```mid fence'}]}}))\n")
    settings = Settings(state_dir=tmp_path / "state",
                        hooks={"PreRun": [HookHandler(command=command)]})
    runner = make_runner(settings=settings)
    first = runner.process_text(
        shell_payload("echo plain", session="cap-2"), "/tmp")
    assert "mid.tool" not in first.result_block
    second = runner.process_text(
        shell_payload("echo learn", session="cap-2"), "/tmp")
    assert "mid.tool" in second.result_block


# -- cap / spill ------------------------------------------------------------------------

def test_output_cap_spills_to_file(make_runner):
    big = "x" * 200_000
    executor = FakeExecutor(output=big)
    runner = make_runner(executor=executor)
    outcome = runner.process_text(shell_payload("make big"), "/tmp")
    block = outcome.result_block
    assert len(block) < 150_000
    assert "truncated at 128k" in block
    output_file = runner.state_dir / "runs" / outcome.run_id / "output.txt"
    assert output_file.read_text() == big


# -- ConfigChange ---------------------------------------------------------------------------

def test_config_change_is_receipted(make_runner, tmp_path):
    state = tmp_path / "state"
    first = make_runner(settings=Settings(state_dir=state))
    first.arm("/tmp")
    changed = Settings(state_dir=state, allowlist=["git status"])
    second = make_runner(settings=changed)
    second.arm("/tmp")
    events = [e["event"] for e in second.ledger.entries()]
    assert "ConfigChange" in events
