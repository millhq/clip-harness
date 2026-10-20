"""Atomic batch task transitions (`action: task-update`).

Copilot's field report: closing one task and starting the next took
two clipboard runs (task-done, then task-doing) and left the session
between states. The batch form replaces the single-task status
actions outright (they were removed — no backward compat): one
payload carries every transition, the goal store validates the whole
set, and either everything applies or nothing does.
"""

from __future__ import annotations

import pytest

from clip_harness.core import RunState
from clip_harness.goals import GoalStore
from clip_harness.payload import (PayloadError, parse_clipboard_text,
                                  parse_payload)

from conftest import FakeExecutor


# -- parser -----------------------------------------------------------------


def test_fenced_task_update_parses_transitions():
    payload = parse_payload(
        "```shell\n"
        "session: velocity-navigator\n"
        "action: task-update\n"
        "done: Implement generic OpenAPI discovery with complete "
        "candidate accounting\n"
        "doing: Normalize canonical contracts and HTTP operations "
        "into generic units\n"
        "```")
    assert payload.action == "task-update"
    assert payload.session_id == "velocity-navigator"
    assert payload.transitions == [
        {"status": "done",
         "title": "Implement generic OpenAPI discovery with complete "
                  "candidate accounting"},
        {"status": "doing",
         "title": "Normalize canonical contracts and HTTP operations "
                  "into generic units"},
    ]
    # The transition lines are consumed by the parser — a task-update
    # payload has no shell command.
    assert payload.command == ""


def test_unfenced_task_update_parses_transitions():
    payload = parse_clipboard_text(
        "session: s1\n"
        "action: task-update\n"
        "done: first task\n"
        "open: second task",
        allow_bare=True)
    assert payload.transitions == [
        {"status": "done", "title": "first task"},
        {"status": "open", "title": "second task"},
    ]
    assert payload.command == ""


def test_task_update_repeated_status_lines():
    payload = parse_payload(
        "```shell\nsession: s1\naction: task-update\n"
        "done: a\ndone: b\ndoing: c\n```")
    assert [t["title"] for t in payload.transitions] == ["a", "b", "c"]
    assert [t["status"] for t in payload.transitions] == [
        "done", "done", "doing"]


def test_task_update_title_may_contain_colons():
    payload = parse_payload(
        "```shell\nsession: s1\naction: task-update\n"
        "done: Fix: the thing\n```")
    assert payload.transitions == [
        {"status": "done", "title": "Fix: the thing"}]


@pytest.mark.parametrize("body", [
    "closed: first task",       # unknown status key
    "done:",                    # missing title
    "first task",               # no status at all
    "session: other",           # a header key is not a transition
])
def test_task_update_malformed_lines_fail_closed(body):
    with pytest.raises(PayloadError):
        parse_payload(
            f"```shell\nsession: s1\naction: task-update\n{body}\n```")
    with pytest.raises(PayloadError):
        parse_clipboard_text(
            f"session: s1\naction: task-update\n{body}",
            allow_bare=True)


def test_task_update_empty_batch_fails_closed():
    with pytest.raises(PayloadError):
        parse_payload("```shell\nsession: s1\naction: task-update\n```")


# -- store: atomic validation -------------------------------------------------


def _store(tmp_path, tasks):
    store = GoalStore(tmp_path / "state", "batch")
    for title, status in tasks:
        store.add_task(title, status)
    return store


def test_store_batch_applies_all_with_from_to(tmp_path):
    store = _store(tmp_path, [("a", "doing"), ("b", "open"),
                              ("c", "done")])
    applied, errors = store.apply_task_transitions([
        {"status": "done", "title": "a"},
        {"status": "doing", "title": "b"},
        {"status": "open", "title": "c"},
    ])
    assert errors == []
    assert applied == [
        {"title": "a", "from": "doing", "to": "done", "noop": False},
        {"title": "b", "from": "open", "to": "doing", "noop": False},
        {"title": "c", "from": "done", "to": "open", "noop": False},
    ]
    assert {t["title"]: t["status"] for t in store.tasks()} == {
        "a": "done", "b": "doing", "c": "open"}


def test_store_noop_reset_is_valid(tmp_path):
    store = _store(tmp_path, [("a", "done")])
    applied, errors = store.apply_task_transitions(
        [{"status": "done", "title": "a"}])
    assert errors == []
    assert applied == [
        {"title": "a", "from": "done", "to": "done", "noop": True}]


def test_store_unknown_title_rejects_everything(tmp_path):
    store = _store(tmp_path, [("a", "open"), ("b", "open")])
    before = store.path.read_bytes()
    applied, errors = store.apply_task_transitions([
        {"status": "done", "title": "a"},
        {"status": "done", "title": "ghost"},
    ])
    assert applied == []
    assert errors == [{"title": "ghost", "problem": "unknown"}]
    # Atomic: the store file is byte-identical, valid entry included.
    assert store.path.read_bytes() == before
    assert {t["title"]: t["status"] for t in store.tasks()} == {
        "a": "open", "b": "open"}


def test_store_duplicate_and_conflicting_titles(tmp_path):
    store = _store(tmp_path, [("a", "open"), ("b", "open")])
    before = store.path.read_bytes()
    _, errors = store.apply_task_transitions([
        {"status": "done", "title": "a"},
        {"status": "done", "title": "a"},
    ])
    assert errors == [{"title": "a", "problem": "duplicate"}]
    _, errors = store.apply_task_transitions([
        {"status": "done", "title": "b"},
        {"status": "doing", "title": "b"},
    ])
    assert errors == [{"title": "b", "problem": "conflicting"}]
    assert store.path.read_bytes() == before


# -- runner: the acceptance case, atomic rejects, the migration ---------------


def _seed_session(runner, session="velocity-navigator"):
    runner.process_text(
        f"```shell\nsession: {session}\naction: task-add\n"
        "Implement generic OpenAPI discovery with complete candidate "
        "accounting\n"
        "Normalize canonical contracts and HTTP operations into "
        "generic units\n"
        "Write the conformance report\n```", "/tmp")
    runner.process_text(
        f"```shell\nsession: {session}\naction: task-update\n"
        "doing: Implement generic OpenAPI discovery with complete "
        "candidate accounting\n"
        "done: Write the conformance report\n```", "/tmp")


def test_batch_transition_end_to_end(make_runner, tmp_path):
    executor = FakeExecutor()
    runner = make_runner(executor=executor)
    _seed_session(runner)
    outcome = runner.process_text(
        "```shell\n"
        "session: velocity-navigator\n"
        "action: task-update\n"
        "done: Implement generic OpenAPI discovery with complete "
        "candidate accounting\n"
        "doing: Normalize canonical contracts and HTTP operations "
        "into generic units\n"
        "open: Write the conformance report\n"
        "```", "/tmp")
    assert outcome.state == RunState.COMPLETED
    # Nothing executed: a task-update payload has no shell command.
    assert executor.calls == []
    block = outcome.result_block
    assert ("task done: Implement generic OpenAPI discovery with "
            "complete candidate accounting (doing → done)") in block
    assert ("task doing: Normalize canonical contracts and HTTP "
            "operations into generic units (open → doing)") in block
    assert ("task open: Write the conformance report "
            "(done → open)") in block
    # One result block; the sticky footer reflects the FINAL state.
    footer = block.split("[open tasks]")[1]
    assert "[doing] Normalize canonical contracts" in footer
    assert "[open] Write the conformance report" in footer
    assert "OpenAPI discovery" not in footer  # done tasks leave it
    store = GoalStore(tmp_path / "state", "velocity-navigator")
    assert {t["title"]: t["status"] for t in store.tasks()} == {
        "Implement generic OpenAPI discovery with complete candidate "
        "accounting": "done",
        "Normalize canonical contracts and HTTP operations into "
        "generic units": "doing",
        "Write the conformance report": "open",
    }
    # One receipt for the batch, carrying the full transition list.
    entries = runner.ledger.entries(run_id=outcome.run_id)
    post = [e for e in entries if e["event"] == "PostRun"]
    assert len(post) == 1
    receipt = post[0]["data"]["tool_response"]
    assert [t["title"] for t in receipt["transitions"]] == [
        "Implement generic OpenAPI discovery with complete candidate "
        "accounting",
        "Normalize canonical contracts and HTTP operations into "
        "generic units",
        "Write the conformance report",
    ]
    assert "errors" not in receipt


def test_batch_with_unknown_title_applies_nothing(make_runner,
                                                   tmp_path):
    executor = FakeExecutor()
    runner = make_runner(executor=executor)
    _seed_session(runner, session="atomic")
    store = GoalStore(tmp_path / "state", "atomic")
    before = store.path.read_bytes()
    outcome = runner.process_text(
        "```shell\nsession: atomic\naction: task-update\n"
        "done: Implement generic OpenAPI discovery with complete "
        "candidate accounting\n"
        "doing: No such task\n```", "/tmp")
    assert executor.calls == []
    block = outcome.result_block
    assert "task-update rejected — nothing applied" in block
    assert "unknown task: No such task" in block
    # The valid transition in the same batch did NOT apply.
    assert store.path.read_bytes() == before
    entries = runner.ledger.entries(run_id=outcome.run_id)
    receipt = [e for e in entries
               if e["event"] == "PostRun"][0]["data"]["tool_response"]
    assert receipt["errors"] == [
        {"title": "No such task", "problem": "unknown"}]


def test_batch_noop_reported_not_failed(make_runner):
    runner = make_runner()
    _seed_session(runner, session="idem")
    outcome = runner.process_text(
        "```shell\nsession: idem\naction: task-update\n"
        "done: Write the conformance report\n```", "/tmp")
    assert outcome.state == RunState.COMPLETED
    assert ("task done: Write the conformance report "
            "(already done — no change)") in outcome.result_block


@pytest.mark.parametrize("removed", ["task-done", "task-doing",
                                     "task-open"])
def test_removed_actions_get_migration_error(make_runner, tmp_path,
                                             removed):
    executor = FakeExecutor()
    runner = make_runner(executor=executor)
    _seed_session(runner, session="mig")
    store = GoalStore(tmp_path / "state", "mig")
    before = store.path.read_bytes()
    outcome = runner.process_text(
        f"```shell\nsession: mig\naction: {removed}\n"
        "Write the conformance report\n```", "/tmp")
    assert outcome.state == RunState.DENIED
    assert executor.calls == []  # the body must NOT run as shell text
    block = outcome.result_block
    assert f"action '{removed}' was removed" in block
    assert "'task-update'" in block
    assert store.path.read_bytes() == before


def test_unfenced_batch_runs_nothing(make_runner):
    executor = FakeExecutor()
    runner = make_runner(executor=executor)
    _seed_session(runner, session="unf")
    outcome = runner.process_text(
        "session: unf\n"
        "action: task-update\n"
        "done: Write the conformance report",
        "/tmp", allow_bare=True)
    assert outcome.state == RunState.COMPLETED
    assert executor.calls == []
    assert ("task done: Write the conformance report "
            "(already done — no change)") in outcome.result_block
