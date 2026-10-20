"""The durable goal contract (0.2.0).

Goals are durable objects in a SQLite ledger (goalstore.py), issued
an immutable goal_uid and REFERENCED by sessions — the Anthropic
shape — replacing the session-owned JSON store outright. This file
covers the ledger (revisions, compare-and-swap, action-id
idempotency, export/import), the new payload headers, the runner
flows (binding, discovery, guards), the one-time legacy migration,
and work Copilot's acceptance case end to end.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest

from clip_harness.core import RunState
from clip_harness.goals import GoalStore
from clip_harness.goalstore import (DuplicateAction, GoalLedger,
                                    GoalStoreError, StaleRevision)
from clip_harness.payload import (PayloadError, parse_clipboard_text,
                                  parse_payload)

from conftest import FakeExecutor


# -- the acceptance fixture: the Navigator goal, as the M365 export --------

NAV_UID = "33476ec7-1cfd-4634-93d6-ab22dde10b23"

NAV_DOCUMENT = {
    "schema": "clip-harness/goal-export@1",
    "exported_at": "2026-10-20T10:15:00-0400",
    "goal": {
        "goal_uid": NAV_UID,
        "human_id": "velocity-navigator",
        "goal": "Build the Velocity Navigator: a discovery-first, "
                "offline repository navigator over the Tangerine "
                "platform repositories",
        "status": "active",
        "revision": 2,
        "plan": {
            "revision_label": "PLAN v2",
            "steps": [
                {"n": 1, "title": "Inventory the platform repositories",
                 "status": "done"},
                {"n": 2, "title": "Run Joern code-fact extraction",
                 "status": "done"},
                {"n": 3, "title": "Gradle supplier discovery with "
                                  "complete candidate accounting",
                 "status": "done"},
                {"n": 4, "title": "Helm supplier discovery with "
                                  "complete candidate accounting",
                 "status": "done"},
                {"n": 5, "title": "OpenAPI supplier: normalize "
                                  "canonical contracts and HTTP "
                                  "operations into generic units",
                 "status": "doing"},
                {"n": 6, "title": "Reconcile suppliers into the "
                                  "canonical model",
                 "status": "open"},
                {"n": 7, "title": "Publish static projections",
                 "status": "open"},
            ],
            "current_step": 5,
        },
        "tasks": [
            {"title": "Implement generic OpenAPI discovery with "
                      "complete candidate accounting",
             "status": "done"},
            {"title": "Normalize canonical contracts and HTTP "
                      "operations into generic units",
             "status": "doing"},
            {"title": "Add the Kustomize supplier", "status": "open"},
        ],
        "backlog": ["Backstage adapter spike", "Maven supplier"],
        "latest_result": "OpenAPI discovery complete: every "
                         "candidate accounted — unaccounted = 0",
        "parent_uid": None,
        "fork_revision": None,
        "provenance": {"imported_from": "m365 harness (SQLite "
                                        "ledger)",
                       "session": "velocity-navigator"},
        "created_at": "2026-10-12T09:00:00-0400",
        "updated_at": "2026-10-19T17:42:00-0400",
    },
}


def _ledger(tmp_path) -> GoalLedger:
    return GoalLedger(tmp_path / "state")


# -- ledger: creation, revisions, bindings -----------------------------------


def test_create_goal_issues_uuid_and_revision_1(tmp_path):
    ledger = _ledger(tmp_path)
    record = ledger.create_goal("Ship the thing", session_id="s1")
    parsed = uuid.UUID(record["goal_uid"])
    assert parsed.version == 4
    # The uid is issued, never derived from the text or the session.
    assert "ship" not in record["goal_uid"]
    assert record["revision"] == 1
    assert record["human_id"] == "ship-the-thing"
    assert record["status"] == "active"
    # The session references the goal; a fresh ledger sees the same.
    assert _ledger(tmp_path).goal_for_session("s1")["goal_uid"] \
        == record["goal_uid"]
    assert _ledger(tmp_path).bound_uid("s1") == record["goal_uid"]


def test_every_mutation_bumps_revision(tmp_path):
    ledger = _ledger(tmp_path)
    record = ledger.create_goal("Goal", session_id="s1")
    uid = record["goal_uid"]
    created_at = record["created_at"]
    record = ledger.set_goal_text(uid, "Goal, revised")
    assert record["revision"] == 2 and record["created_at"] == created_at
    added, record = ledger.add_tasks(uid, ["one", "two"])
    assert record["revision"] == 3 and len(added) == 2
    task, record = ledger.set_task_status(uid, "one", "doing")
    assert task["status"] == "doing" and record["revision"] == 4
    record = ledger.set_status(uid, "paused")
    assert record["revision"] == 5 and record["status"] == "paused"
    # Unknown task title: no mutation, no bump.
    task, record = ledger.set_task_status(uid, "ghost", "done")
    assert task is None and record["revision"] == 5


def test_set_status_rejects_unsettable_and_bogus(tmp_path):
    ledger = _ledger(tmp_path)
    uid = ledger.create_goal("Goal")["goal_uid"]
    with pytest.raises(GoalStoreError):
        ledger.set_status(uid, "branched")  # preserved-only status
    with pytest.raises(GoalStoreError):
        ledger.set_status(uid, "bogus")


# -- ledger: compare-and-swap + idempotency -----------------------------------


def test_cas_guard_on_task_update(tmp_path):
    ledger = _ledger(tmp_path)
    uid = ledger.create_goal("Goal")["goal_uid"]
    ledger.add_tasks(uid, ["a", "b"])  # revision now 2
    with pytest.raises(StaleRevision) as caught:
        ledger.apply_task_transitions(
            uid, [{"status": "done", "title": "a"}], expect_revision=9)
    assert caught.value.expected == 9 and caught.value.actual == 2
    assert ledger.get(uid)["revision"] == 2  # nothing applied
    applied, errors, record = ledger.apply_task_transitions(
        uid, [{"status": "done", "title": "a"}], expect_revision=2)
    assert errors == [] and record["revision"] == 3


def test_duplicate_action_id_rejected_before_mutation(tmp_path):
    ledger = _ledger(tmp_path)
    uid = ledger.create_goal("Goal")["goal_uid"]
    ledger.add_tasks(uid, ["a"])
    action_id = str(uuid.uuid4())
    ledger.apply_task_transitions(
        uid, [{"status": "done", "title": "a"}], action_id=action_id,
        outcome="task-update applied")
    before = ledger.get(uid)
    with pytest.raises(DuplicateAction) as caught:
        ledger.apply_task_transitions(
            uid, [{"status": "open", "title": "a"}],
            action_id=action_id)
    assert "task-update applied" in caught.value.outcome
    assert ledger.get(uid) == before  # not even a revision bump
    # The read-side check the runner uses sees it under the goal scope.
    assert ledger.action_outcome([uid], action_id) is not None
    assert ledger.action_outcome([uid], str(uuid.uuid4())) is None


# -- ledger: export / import ----------------------------------------------------


def test_export_import_round_trip_is_exact(tmp_path):
    source = _ledger(tmp_path)
    record = source.create_goal("Round trip", session_id="s1")
    uid = record["goal_uid"]
    source.add_tasks(uid, ["one"])
    source.set_task_status(uid, "one", "doing")
    document = source.export_document(uid)

    target = GoalLedger(tmp_path / "other-state")
    imported = target.import_document(document)
    assert imported == [target.get(uid)]
    assert imported[0] == source.get(uid)  # every field, verbatim


def test_import_refuses_existing_uid_and_bad_documents(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.import_document(NAV_DOCUMENT)
    with pytest.raises(GoalStoreError) as caught:
        ledger.import_document(NAV_DOCUMENT)
    assert NAV_UID in str(caught.value)  # the refusal names the uid
    with pytest.raises(GoalStoreError):
        ledger.import_document({"schema": "nope", "goal": {}})
    broken = json.loads(json.dumps(NAV_DOCUMENT))
    broken["goal"]["goal_uid"] = "aaaaaaaa-1111-4111-8111-111111111111"
    broken["goal"]["status"] = "bogus"
    with pytest.raises(GoalStoreError):
        ledger.import_document(broken)
    # The failed imports changed nothing.
    assert [g["goal_uid"] for g in ledger.list_goals()] == [NAV_UID]


def test_export_all_and_multi_import(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.create_goal("One")
    ledger.create_goal("Two")
    document = ledger.export_all_document()
    assert len(document["goals"]) == 2
    target = GoalLedger(tmp_path / "elsewhere")
    assert len(target.import_document(document)) == 2


# -- legacy migration -----------------------------------------------------------


def _write_legacy(state: Path, name: str, data: dict) -> None:
    goals_dir = state / "goals"
    goals_dir.mkdir(parents=True, exist_ok=True)
    (goals_dir / f"{name}.json").write_text(
        json.dumps(data), encoding="utf-8")


def test_legacy_session_store_migrates_once(tmp_path):
    state = tmp_path / "state"
    _write_legacy(state, "old-work",
                  {"goal": {"title": "Legacy goal", "status": "done"},
                   "tasks": [{"title": "t1", "status": "doing"},
                             {"title": "t2", "status": "open"}]})
    _write_legacy(state, "tasks-only",
                  {"goal": None,
                   "tasks": [{"title": "loose", "status": "open"}]})
    _write_legacy(state, "empty", {"goal": None, "tasks": []})
    ledger = GoalLedger(state)
    assert len(ledger.migration_notes) == 2  # the empty file has none
    records = {r["human_id"]: r for r in ledger.list_goals()}
    legacy = records["legacy-goal"]
    assert legacy["status"] == "complete"  # legacy done -> complete
    assert [t["title"] for t in legacy["tasks"]] == ["t1", "t2"]
    assert legacy["provenance"]["session"] == "old-work"
    # Sessions keep working through their binding.
    store = GoalStore(state, "old-work")
    assert store.goal()["title"] == "Legacy goal"
    assert store.goal()["status"] == "complete"
    assert GoalStore(state, "tasks-only").tasks()[0]["title"] == "loose"
    # Files are renamed, never read again; a second open is a no-op.
    assert not (state / "goals" / "old-work.json").exists()
    assert (state / "goals" / "old-work.json.migrated").exists()
    again = GoalLedger(state)
    assert again.migration_notes == []
    assert len(again.list_goals()) == 2


# -- parser: the new headers -----------------------------------------------------


@pytest.mark.parametrize("unfenced", [False, True])
def test_goal_headers_parse_fenced_and_unfenced(unfenced):
    body = ("session: s1\n"
            "goal: 33476ec7-1cfd-4634-93d6-ab22dde10b23\n"
            "expect-revision: 2\n"
            "action-id: 9b2f2f9c-1111-4111-8111-111111111111\n"
            "echo hi")
    text = body if unfenced else f"```shell\n{body}\n```"
    payload = parse_clipboard_text(text, allow_bare=True)
    assert payload.goal_uid == NAV_UID
    assert payload.expect_revision == 2
    assert payload.action_id == "9b2f2f9c-1111-4111-8111-111111111111"
    assert payload.command == "echo hi"  # headers never reach the shell


@pytest.mark.parametrize("bad", ["expect-revision: two",
                                 "expect-revision: 2.5",
                                 "action-id:"])
def test_malformed_goal_headers_fail_closed(bad):
    text = f"```shell\nsession: s1\n{bad}\necho hi\n```"
    with pytest.raises(PayloadError):
        parse_payload(text)
    with pytest.raises(PayloadError):
        parse_clipboard_text(f"session: s1\n{bad}\necho hi",
                             allow_bare=True)


@pytest.mark.parametrize("action", ["goal-list", "goal-show"])
def test_bodiless_goal_actions_parse(action):
    fenced = parse_payload(
        f"```shell\nsession: s1\naction: {action}\n```")
    assert fenced.action == action and fenced.command == ""
    unfenced = parse_clipboard_text(f"session: s1\naction: {action}",
                                    allow_bare=True)
    assert unfenced.action == action


# -- runner: creation, binding, projection ----------------------------------------


def _goal_set(runner, session="work", text="Ship the durable thing"):
    return runner.process_text(
        f"```shell\nsession: {session}\naction: goal-set\n{text}\n```",
        "/tmp")


def test_goal_set_creates_durable_goal_and_teaches_uid(make_runner,
                                                       tmp_path):
    runner = make_runner()
    outcome = _goal_set(runner)
    assert outcome.state == RunState.COMPLETED
    match = re.search(r"goal_uid: ([0-9a-f-]{36})", outcome.result_block)
    assert match, outcome.result_block
    uid = match.group(1)
    assert "revision 1 · created" in outcome.result_block
    # The same block's footer already projects the durable record.
    assert f"({uid[:8]})" in outcome.result_block
    # A later bound run — even from a brand-new runner instance over
    # the same state dir — carries the same goal and uid.
    runner2 = make_runner()
    outcome2 = runner2.process_text(
        "```shell\nsession: work\necho next\n```", "/tmp")
    assert "Ship the durable thing [active]" in outcome2.result_block
    assert f"({uid[:8]})" in outcome2.result_block
    # A second goal-set UPDATES the same goal (no new uid).
    outcome3 = _goal_set(runner2, text="Ship the durable thing, v2")
    assert f"goal_uid: {uid}" in outcome3.result_block
    assert "revision 2" in outcome3.result_block
    assert len(GoalLedger(tmp_path / "state").list_goals()) == 1


def test_goal_header_binds_shell_run_and_footer_projects(make_runner,
                                                          tmp_path):
    ledger = GoalLedger(tmp_path / "state")
    ledger.import_document(NAV_DOCUMENT)
    executor = FakeExecutor()
    runner = make_runner(executor=executor)
    outcome = runner.process_text(
        f"```shell\nsession: some-other-session\ngoal: {NAV_UID}\n"
        "echo working\n```", "/tmp")
    assert outcome.state == RunState.COMPLETED
    assert executor.calls == ["echo working"]
    block = outcome.result_block
    assert "Build the Velocity Navigator" in block
    assert "(33476ec7)" in block
    # The plan projects a [step] line between [goal] and [open tasks].
    lines = block.rstrip().splitlines()
    if lines and lines[-1] == "```":
        lines = lines[:-1]
    assert lines[-3].startswith("[goal] ")
    assert lines[-2].startswith("[step] 5 — OpenAPI supplier:")
    assert lines[-1].startswith("[open tasks] ")


def test_unknown_goal_header_denies_shell_run(make_runner):
    executor = FakeExecutor()
    runner = make_runner(executor=executor)
    outcome = runner.process_text(
        "```shell\nsession: s1\n"
        "goal: 00000000-0000-4000-8000-000000000000\necho hi\n```",
        "/tmp")
    assert outcome.state == RunState.DENIED
    assert "unknown goal_uid" in outcome.result_block
    assert executor.calls == []


def test_no_plan_no_step_line(make_runner):
    runner = make_runner()
    _goal_set(runner)
    outcome = runner.process_text(
        "```shell\nsession: work\necho hi\n```", "/tmp")
    assert "[step]" not in outcome.result_block


# -- runner: discovery after total session loss -----------------------------------


def test_goal_list_and_show_discover_after_session_loss(make_runner,
                                                        tmp_path):
    GoalLedger(tmp_path / "state").import_document(NAV_DOCUMENT)
    runner = make_runner()
    # A fresh session that has never heard of the goal:
    listed = runner.process_text(
        "```shell\nsession: fresh-eyes\naction: goal-list\n```", "/tmp")
    assert listed.state == RunState.COMPLETED
    assert NAV_UID in listed.result_block
    assert "velocity-navigator · active · rev 2" in listed.result_block
    assert "step 5: OpenAPI supplier" in listed.result_block
    shown = runner.process_text(
        f"```shell\nsession: fresh-eyes\naction: goal-show\n"
        f"goal: {NAV_UID}\n```", "/tmp")
    block = shown.result_block
    for needle in ("PLAN v2", "current step 5",
                   "Run Joern code-fact extraction",
                   "Backstage adapter spike",
                   "unaccounted = 0", "revision: 2",
                   "m365 harness"):
        assert needle in block, f"{needle!r} missing from:\n{block}"
    # Discovery is read-only: the record is untouched.
    assert GoalLedger(tmp_path / "state").get(NAV_UID)["revision"] == 2
    # No session at all also works for the read-only ops.
    anon = runner.process_text("```shell\naction: goal-list\n```",
                               "/tmp")
    assert anon.state == RunState.COMPLETED
    assert NAV_UID in anon.result_block


def test_goal_show_unknown_uid_renders_not_denies(make_runner):
    runner = make_runner()
    outcome = runner.process_text(
        "```shell\naction: goal-show\n"
        "goal: 00000000-0000-4000-8000-000000000000\n```", "/tmp")
    assert outcome.state == RunState.COMPLETED
    assert "no such goal:" in outcome.result_block


# -- runner: guards (CAS + idempotency) --------------------------------------------


def test_goal_show_defaults_to_sessions_bound_goal(make_runner):
    runner = make_runner()
    _goal_set(runner)
    shown = runner.process_text(
        "```shell\nsession: work\naction: goal-show\n```", "/tmp")
    assert "Ship the durable thing" in shown.result_block
    assert "revision: 1" in shown.result_block


def test_goal_set_honors_expect_revision(make_runner):
    runner = make_runner()
    _goal_set(runner)
    stale = runner.process_text(
        "```shell\nsession: work\naction: goal-set\n"
        "expect-revision: 9\nNew text\n```", "/tmp")
    assert "stale revision: expected 9" in stale.result_block
    assert "current revision is 1" in stale.result_block
    applied = runner.process_text(
        "```shell\nsession: work\naction: goal-set\n"
        "expect-revision: 1\nNew text\n```", "/tmp")
    assert "New text" in applied.result_block
    assert "revision 2" in applied.result_block


def test_expect_revision_asserts_on_shell_runs(make_runner, tmp_path):
    GoalLedger(tmp_path / "state").import_document(NAV_DOCUMENT)
    executor = FakeExecutor()
    runner = make_runner(executor=executor)
    ok = runner.process_text(
        f"```shell\nsession: s9\ngoal: {NAV_UID}\nexpect-revision: 2\n"
        "echo guarded\n```", "/tmp")
    assert ok.state == RunState.COMPLETED
    stale = runner.process_text(
        f"```shell\nsession: s9\ngoal: {NAV_UID}\nexpect-revision: 7\n"
        "echo stale\n```", "/tmp")
    assert stale.state == RunState.DENIED
    assert "stale revision: expected 7" in stale.result_block
    assert "is at revision 2" in stale.result_block
    assert executor.calls == ["echo guarded"]


def test_action_id_makes_shell_run_replay_safe(make_runner):
    executor = FakeExecutor()
    runner = make_runner(executor=executor)
    payload = ("```shell\nsession: s7\n"
               "action-id: 12345678-1234-4234-8234-123456789012\n"
               "echo once\n```")
    first = runner.process_text(payload, "/tmp")
    assert first.state == RunState.COMPLETED
    replay = runner.process_text(payload, "/tmp")
    assert replay.state == RunState.COMPLETED
    assert "duplicate action-id" in replay.result_block
    assert "nothing was executed" in replay.result_block
    assert executor.calls == ["echo once"]  # ran exactly once


def test_goal_status_payload_uses_durable_statuses(make_runner):
    runner = make_runner()
    _goal_set(runner)
    outcome = runner.process_text(
        "```shell\nsession: work\naction: goal-status\ncomplete\n```",
        "/tmp")
    assert "goal status: complete" in outcome.result_block
    following = runner.process_text(
        "```shell\nsession: work\necho after\n```", "/tmp")
    assert "[complete]" in following.result_block


# -- the acceptance case, end to end -------------------------------------------------


def test_acceptance_navigator_goal(make_runner, tmp_path):
    """Copilot's acceptance test, as specified: import at revision 2,
    lose every session, discover by list, resume by uid, guarded
    update to revision 3, stale retry rejected, action-id replay
    rejected, unrelated goal untouched throughout."""
    state = tmp_path / "state"
    ledger = GoalLedger(state)
    ledger.import_document(NAV_DOCUMENT)
    unrelated = ledger.create_goal("Unrelated goal", session_id="other")
    unrelated_before = ledger.export_document(
        unrelated["goal_uid"])["goal"]

    executor = FakeExecutor()
    runner = make_runner(executor=executor)

    # Total session loss: no session named velocity-navigator exists
    # in this state (no sessions dir was ever written); a fresh
    # session discovers the goal by list and resumes by exact uid.
    listed = runner.process_text(
        "```shell\nsession: brand-new\naction: goal-list\n```", "/tmp")
    assert NAV_UID in listed.result_block
    shown = runner.process_text(
        f"```shell\nsession: brand-new\naction: goal-show\n"
        f"goal: {NAV_UID}\n```", "/tmp")
    for needle in ("PLAN v2", "current step 5",
                   "Run Joern code-fact extraction",      # Joern done
                   "Gradle supplier discovery",           # Gradle done
                   "Helm supplier discovery",             # Helm done
                   "Backstage adapter spike",             # backlog
                   "revision: 2"):
        assert needle in shown.result_block, needle

    # The revision-guarded update: expect 2 -> applies -> revision 3.
    action_id = "aaaaaaaa-0000-4000-8000-000000000001"
    payload = (
        f"```shell\nsession: brand-new\ngoal: {NAV_UID}\n"
        f"action: task-update\nexpect-revision: 2\n"
        f"action-id: {action_id}\n"
        "done: Normalize canonical contracts and HTTP operations "
        "into generic units\n"
        "doing: Add the Kustomize supplier\n```")
    applied = runner.process_text(payload, "/tmp")
    assert applied.state == RunState.COMPLETED
    assert "task done: Normalize canonical contracts" in \
        applied.result_block
    assert "task doing: Add the Kustomize supplier" in \
        applied.result_block
    record = GoalLedger(state).get(NAV_UID)
    assert record["revision"] == 3
    assert {t["title"]: t["status"] for t in record["tasks"]}[
        "Normalize canonical contracts and HTTP operations into "
        "generic units"] == "done"

    # Replaying the SAME payload (same action id) is a duplicate —
    # rejected before execution, record untouched.
    replay = runner.process_text(payload, "/tmp")
    assert "duplicate action-id" in replay.result_block
    assert GoalLedger(state).get(NAV_UID)["revision"] == 3

    # A fresh action id with the now-stale expectation is rejected
    # as stale, naming both revisions, mutating nothing.
    stale = runner.process_text(
        f"```shell\nsession: brand-new\ngoal: {NAV_UID}\n"
        "action: task-update\nexpect-revision: 2\n"
        "action-id: bbbbbbbb-0000-4000-8000-000000000002\n"
        "open: Add the Kustomize supplier\n```", "/tmp")
    assert "stale revision: expected 2" in stale.result_block
    assert "current revision is 3" in stale.result_block
    record = GoalLedger(state).get(NAV_UID)
    assert record["revision"] == 3
    assert {t["title"]: t["status"] for t in record["tasks"]}[
        "Add the Kustomize supplier"] == "doing"  # not reopened

    # Nothing ever executed, and the unrelated goal is byte-identical
    # (field-for-field) throughout.
    assert executor.calls == []
    assert GoalLedger(state).export_document(
        unrelated["goal_uid"])["goal"] == unrelated_before
