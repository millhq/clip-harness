"""The durable goal contract (0.3.0).

Goal, plan and task are three separate things, following
Anthropic's model: the GOAL is the anchor (a durable object in a
file store — goalstore.py — issued an immutable goal_uid and
REFERENCED by sessions, never owned by them); a TASK is its own
object and the only tracked execution state; a PLAN is a Markdown
DOCUMENT, authored and replaced whole, never parsed. This file
covers the store (revisions, compare-and-swap, action-id
idempotency, export/import), the plan actions, the payload
headers, the runner flows (binding, discovery, guards), both
one-time migrations (the 0.2.x SQLite ledger and the pre-0.2.0
legacy session files), and work Copilot's acceptance case end
to end.
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

# The plan is a DOCUMENT now — this string is the whole of it, and
# its title line ("# PLAN v2") is its version identity.
NAV_PLAN = (
    "# PLAN v2\n"
    "\n"
    "1. [done] Inventory the platform repositories\n"
    "2. [done] Run Joern code-fact extraction\n"
    "3. [done] Gradle supplier discovery with complete candidate "
    "accounting\n"
    "4. [done] Helm supplier discovery with complete candidate "
    "accounting\n"
    "5. [doing] OpenAPI supplier: normalize canonical contracts "
    "and HTTP operations into generic units\n"
    "6. [open] Reconcile suppliers into the canonical model\n"
    "7. [open] Publish static projections")

NAV_DOCUMENT = {
    "format": "clip-harness/goal-export@2",
    "exported_at": "2026-10-20T10:15:00-0400",
    "goal": {
        "goal_uid": NAV_UID,
        "human_id": "velocity-navigator",
        "goal": "Build the Velocity Navigator: a discovery-first, "
                "offline repository navigator over the Tangerine "
                "platform repositories",
        "status": "active",
        "revision": 2,
        "latest_result": "OpenAPI discovery complete: every "
                         "candidate accounted — unaccounted = 0",
        "provenance": {"imported_from": "m365 harness",
                       "session": "velocity-navigator"},
        "created_at": "2026-10-12T09:00:00-0400",
        "updated_at": "2026-10-19T17:42:00-0400",
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
    "plan": NAV_PLAN,
}


def _ledger(tmp_path) -> GoalLedger:
    return GoalLedger(tmp_path / "state")


# -- store: creation, revisions, bindings -----------------------------------


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


# -- store: compare-and-swap + idempotency -----------------------------------


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


# -- store: export / import ----------------------------------------------------


def test_export_import_round_trip_is_exact(tmp_path):
    source = _ledger(tmp_path)
    record = source.create_goal("Round trip", session_id="s1")
    uid = record["goal_uid"]
    source.add_tasks(uid, ["one"])
    source.set_task_status(uid, "one", "doing")
    source.set_plan(uid, "# PLAN v1\n\n1. [doing] one")
    document = source.export_document(uid)
    assert document["format"] == "clip-harness/goal-export@2"
    assert document["plan"] == "# PLAN v1\n\n1. [doing] one"
    assert document["tasks"] == [{"title": "one", "status": "doing"}]
    assert "tasks" not in document["goal"]  # the goal is slim

    target = GoalLedger(tmp_path / "other-state")
    imported = target.import_document(document)
    assert imported[0]["goal_uid"] == uid
    # Re-exporting the import reproduces the document exactly
    # (modulo the export timestamp): goal fields, tasks and plan
    # all carried. Task ids are NOT carried — tasks are recreated
    # as fresh objects in the new store.
    again = target.export_document(uid)
    assert {k: v for k, v in again.items() if k != "exported_at"} \
        == {k: v for k, v in document.items() if k != "exported_at"}
    source_task_id = source.get(uid)["tasks"][0]["task_id"]
    assert imported[0]["tasks"][0]["task_id"] != source_task_id


def test_import_refuses_existing_uid_and_bad_documents(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.import_document(NAV_DOCUMENT)
    with pytest.raises(GoalStoreError) as caught:
        ledger.import_document(NAV_DOCUMENT)
    assert NAV_UID in str(caught.value)  # the refusal names the uid
    with pytest.raises(GoalStoreError):
        ledger.import_document({"format": "nope", "goal": {}})
    broken = json.loads(json.dumps(NAV_DOCUMENT))
    broken["goal"]["goal_uid"] = "aaaaaaaa-1111-4111-8111-111111111111"
    broken["goal"]["status"] = "bogus"
    with pytest.raises(GoalStoreError):
        ledger.import_document(broken)
    # The failed imports changed nothing.
    assert [g["goal_uid"] for g in ledger.list_goals()] == [NAV_UID]


def test_import_rejects_retired_v1_documents(tmp_path):
    """@1 support is dropped outright (it never carried real
    data): the error names the situation and points at @2."""
    ledger = _ledger(tmp_path)
    v1 = {"schema": "clip-harness/goal-export@1",
          "goal": {"goal_uid": NAV_UID}}
    with pytest.raises(GoalStoreError) as caught:
        ledger.import_document(v1)
    message = str(caught.value)
    assert "goal-export@1" in message and "retired" in message
    assert "goal-export@2" in message
    assert ledger.list_goals() == []


def test_export_all_and_multi_import(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.create_goal("One")
    ledger.create_goal("Two")
    document = ledger.export_all_document()
    assert len(document["goals"]) == 2
    target = GoalLedger(tmp_path / "elsewhere")
    assert len(target.import_document(document)) == 2


# -- plan: a document, authored and replaced whole ------------------------------


def test_plan_set_replaces_whole_document(tmp_path):
    ledger = _ledger(tmp_path)
    state = tmp_path / "state"
    uid = ledger.create_goal("Planned")["goal_uid"]
    assert ledger.get_plan(uid) is None
    assert ledger.get(uid)["plan"] is None

    record = ledger.set_plan(uid, "# PLAN v1\n\n1. [open] first")
    assert record["revision"] == 2  # plan-set is a goal mutation
    assert record["plan"] == "# PLAN v1\n\n1. [open] first"
    # The document lives at its file home, verbatim (+ the file's
    # one trailing newline).
    plan_file = state / "goals" / uid / "plan.md"
    assert plan_file.read_text(encoding="utf-8") \
        == "# PLAN v1\n\n1. [open] first\n"
    # Replacement is whole: no trace of v1 survives a v2.
    record = ledger.set_plan(uid, "# PLAN v2\n\nfree-form prose, "
                                  "no steps at all")
    assert record["revision"] == 3
    assert ledger.get_plan(uid) == "# PLAN v2\n\nfree-form prose, " \
                                   "no steps at all"
    # The harness never parses it: arbitrary markdown round-trips.
    weird = "# T\n\n- [ ] not a step\n\n```\ncode\n```\n\ntrailing  "
    ledger.set_plan(uid, weird)
    assert ledger.get_plan(uid) == weird.rstrip("\n")


def test_plan_set_refuses_empty_and_honors_guards(tmp_path):
    ledger = _ledger(tmp_path)
    uid = ledger.create_goal("Guarded plan")["goal_uid"]
    with pytest.raises(GoalStoreError):
        ledger.set_plan(uid, "   \n  ")
    assert ledger.get(uid)["revision"] == 1  # nothing happened
    with pytest.raises(StaleRevision):
        ledger.set_plan(uid, "# PLAN v1", expect_revision=7)
    action_id = str(uuid.uuid4())
    ledger.set_plan(uid, "# PLAN v1", action_id=action_id,
                    outcome="plan set")
    with pytest.raises(DuplicateAction) as caught:
        ledger.set_plan(uid, "# PLAN v2", action_id=action_id)
    assert "plan set" in caught.value.outcome
    assert ledger.get_plan(uid) == "# PLAN v1"  # the replay lost


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


def _construct_ledger_barriered(state_dir: str, barrier, queue) -> None:
    try:
        barrier.wait(timeout=15)
        GoalLedger(state_dir)
        queue.put("")
    except Exception as exc:  # reported to the parent for assertion
        queue.put(f"{type(exc).__name__}: {exc}")


def test_concurrent_first_construction_races_safely(tmp_path):
    """Regression (release gates 0.2.0/0.2.1, E2E case 33; retargeted
    to the file store in 0.3.0): two processes constructing the
    store on one FRESH state dir at the same instant. In the SQLite
    era the loser died at startup (WAL pragma / migration marker
    races); on files the hazard is a double migration import.
    Barrier-synchronized construction must succeed in both
    processes, with the legacy goal imported exactly once — the
    migration runs under the store lock and the loser finds
    nothing left to migrate.
    """
    import multiprocessing as mp

    for trial in range(3):
        state = tmp_path / f"state-{trial}"
        _write_legacy(state, "race-work",
                      {"goal": {"title": "Race goal", "status": "active"},
                       "tasks": [{"title": "t1", "status": "open"}]})
        barrier = mp.Barrier(2)
        queue = mp.Queue()
        procs = [mp.Process(target=_construct_ledger_barriered,
                            args=(str(state), barrier, queue))
                 for _ in range(2)]
        for proc in procs:
            proc.start()
        errors = [queue.get(timeout=30) for _ in range(2)]
        for proc in procs:
            proc.join(timeout=30)
        assert errors == ["", ""], f"trial {trial}: {errors}"
        ledger = GoalLedger(state)
        assert [r["goal"] for r in ledger.list_goals()] == ["Race goal"]
        assert (state / "goals" / "race-work.json.migrated").exists()


def _task_update_barriered(state_dir: str, uid: str, title: str,
                           barrier, queue) -> None:
    try:
        ledger = GoalLedger(state_dir)
        barrier.wait(timeout=15)
        ledger.apply_task_transitions(
            uid, [{"status": "done", "title": title}])
        queue.put("")
    except Exception as exc:  # reported to the parent for assertion
        queue.put(f"{type(exc).__name__}: {exc}")


def test_concurrent_task_updates_lose_nothing(tmp_path):
    """Two processes task-update DIFFERENT tasks of the SAME goal
    at the same instant: the per-goal lock serializes the
    mutations — both apply, the revision advances by exactly 2,
    and neither update is lost."""
    import multiprocessing as mp

    state = tmp_path / "state"
    ledger = GoalLedger(state)
    uid = ledger.create_goal("Shared goal")["goal_uid"]
    ledger.add_tasks(uid, ["task a", "task b"])  # revision 2
    barrier = mp.Barrier(2)
    queue = mp.Queue()
    procs = [mp.Process(target=_task_update_barriered,
                        args=(str(state), uid, title, barrier, queue))
             for title in ("task a", "task b")]
    for proc in procs:
        proc.start()
    errors = [queue.get(timeout=30) for _ in range(2)]
    for proc in procs:
        proc.join(timeout=30)
    assert errors == ["", ""]
    record = GoalLedger(state).get(uid)
    assert record["revision"] == 4  # 2 (setup) + 2 (one per update)
    assert {t["title"]: t["status"] for t in record["tasks"]} \
        == {"task a": "done", "task b": "done"}


# -- the 0.2.x SQLite ledger migration ------------------------------------------


def _seed_sqlite_ledger(state: Path) -> None:
    """A 0.2.x goals.db, built with the old schema (from git
    history — the fused record with plan/tasks/backlog columns)."""
    import sqlite3

    state.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state / "goals.db"))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE goals (
                goal_uid TEXT PRIMARY KEY,
                human_id TEXT NOT NULL,
                goal TEXT NOT NULL,
                status TEXT NOT NULL,
                revision INTEGER NOT NULL,
                plan TEXT,
                tasks TEXT NOT NULL,
                backlog TEXT NOT NULL,
                latest_result TEXT NOT NULL DEFAULT '',
                parent_uid TEXT,
                fork_revision INTEGER,
                provenance TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL);
            CREATE TABLE session_bindings (
                session_id TEXT PRIMARY KEY,
                goal_uid TEXT NOT NULL,
                bound_at TEXT NOT NULL);
            CREATE TABLE applied_actions (
                scope TEXT NOT NULL,
                action_id TEXT NOT NULL,
                applied_at TEXT NOT NULL,
                outcome TEXT NOT NULL,
                PRIMARY KEY (scope, action_id));
            """)
        plan = {"revision_label": "PLAN v2",
                "steps": [
                    {"n": 1, "title": "Inventory repositories",
                     "status": "done"},
                    {"n": 2, "title": "Extract code facts",
                     "status": "doing"}],
                "current_step": 2}
        conn.execute(
            "INSERT INTO goals (goal_uid, human_id, goal, status, "
            "revision, plan, tasks, backlog, latest_result, "
            "parent_uid, fork_revision, provenance, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?)",
            (NAV_UID, "velocity-navigator", "Build the Navigator",
             "active", 2, json.dumps(plan),
             json.dumps([{"title": "Supplier discovery",
                          "status": "doing"}]),
             json.dumps(["Backstage spike", "Maven supplier"]),
             "discovery complete", None, None,
             json.dumps({"session": "velocity-navigator"}),
             "2026-10-12T09:00:00-0400", "2026-10-19T17:42:00-0400"))
        conn.execute(
            "INSERT INTO session_bindings VALUES (?, ?, ?)",
            ("velocity-navigator", NAV_UID, "2026-10-12T09:01:00-0400"))
        conn.execute(
            "INSERT INTO applied_actions VALUES (?, ?, ?, ?)",
            (NAV_UID, "cccccccc-0000-4000-8000-000000000003",
             "2026-10-19T17:42:00-0400", "task-update applied"))
        conn.commit()
    finally:
        conn.close()


def test_sqlite_ledger_migrates_to_files(tmp_path):
    state = tmp_path / "state"
    _seed_sqlite_ledger(state)
    ledger = GoalLedger(state)
    goal_dir = state / "goals" / NAV_UID
    # The file layout exists; the database is retired.
    assert (goal_dir / "goal.json").exists()
    assert not (state / "goals.db").exists()
    assert (state / "goals.db.migrated").exists()
    record = ledger.get(NAV_UID)
    assert record["goal"] == "Build the Navigator"
    assert record["revision"] == 2
    assert record["latest_result"] == "discovery complete"
    # Tasks became task files; backlog entries follow as open
    # tasks, in order.
    assert [(t["title"], t["status"], t["position"])
            for t in record["tasks"]] == [
        ("Supplier discovery", "doing", 1),
        ("Backstage spike", "open", 2),
        ("Maven supplier", "open", 3)]
    assert len(list((goal_dir / "tasks").glob("*.json"))) == 3
    # The plan dict was RENDERED to a document: title line from
    # the revision label, one rendered line per step.
    assert record["plan"] == (
        "# PLAN v2\n\n"
        "1. [done] Inventory repositories\n"
        "2. [doing] Extract code facts")
    assert (goal_dir / "plan.md").exists()
    # The binding and the applied action-id carried over: the
    # session still resolves, and the replay guard still bites.
    assert ledger.bound_uid("velocity-navigator") == NAV_UID
    assert ledger.action_outcome(
        [NAV_UID], "cccccccc-0000-4000-8000-000000000003") \
        == "task-update applied"
    assert any(note.get("goal_uid") == NAV_UID
               for note in ledger.migration_notes)
    # A second open is a no-op: nothing re-migrates, nothing moves.
    again = GoalLedger(state)
    assert again.migration_notes == []
    assert again.get(NAV_UID)["revision"] == 2


def test_sqlite_and_legacy_migration_dedupes_by_binding(tmp_path):
    """Both stores present: the SQLite ledger migrates first; a
    legacy session file whose session is already bound is skipped
    (still renamed — never re-read), not double-imported."""
    state = tmp_path / "state"
    _seed_sqlite_ledger(state)
    _write_legacy(state, "velocity-navigator",
                  {"goal": {"title": "Stale legacy copy",
                            "status": "active"},
                   "tasks": [{"title": "old", "status": "open"}]})
    _write_legacy(state, "solo-legacy",
                  {"goal": {"title": "Solo legacy", "status": "active"},
                   "tasks": []})
    ledger = GoalLedger(state)
    goals = {g["human_id"] for g in ledger.list_goals()}
    assert goals == {"velocity-navigator", "solo-legacy"}
    assert not (state / "goals" / "velocity-navigator.json").exists()
    assert (state / "goals"
            / "velocity-navigator.json.migrated").exists()


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
    # The footer is exactly two lines — [goal] then [open tasks].
    # There is NO [step] line (0.3.0 removed the stored current
    # step); the in-progress task shows as [doing] in [open tasks].
    lines = block.rstrip().splitlines()
    if lines and lines[-1] == "```":
        lines = lines[:-1]
    assert lines[-2].startswith("[goal] ")
    assert lines[-1].startswith("[open tasks] ")
    assert "[doing] Normalize canonical contracts" in lines[-1]
    assert "[step]" not in block


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


def test_footer_never_has_a_step_line(make_runner):
    runner = make_runner()
    _goal_set(runner)
    runner.process_text(
        "```shell\nsession: work\naction: plan-set\n"
        "# PLAN v1\n\n1. [doing] The only step\n```", "/tmp")
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
    shown = runner.process_text(
        f"```shell\nsession: fresh-eyes\naction: goal-show\n"
        f"goal: {NAV_UID}\n```", "/tmp")
    block = shown.result_block
    # goal-show reconciles the goal and its tasks, and POINTS at
    # the plan (its title line) without embedding the document.
    for needle in ("PLAN v2", "Add the Kustomize supplier",
                   "unaccounted = 0", "revision: 2", "m365 harness"):
        assert needle in block, f"{needle!r} missing from:\n{block}"
    assert "Run Joern code-fact extraction" not in block
    # The document itself comes back verbatim via plan-show.
    plan = runner.process_text(
        f"```shell\nsession: fresh-eyes\naction: plan-show\n"
        f"goal: {NAV_UID}\n```", "/tmp")
    assert NAV_PLAN in plan.result_block
    for needle in ("Run Joern code-fact extraction",
                   "Gradle supplier discovery",
                   "Helm supplier discovery"):
        assert needle in plan.result_block
    # Discovery is read-only: the record is untouched.
    assert GoalLedger(tmp_path / "state").get(NAV_UID)["revision"] == 2
    # No session at all also works for the read-only ops.
    anon = runner.process_text("```shell\naction: goal-list\n```",
                               "/tmp")
    assert anon.state == RunState.COMPLETED
    assert NAV_UID in anon.result_block


def test_plan_actions_through_runner(make_runner, tmp_path):
    runner = make_runner()
    _goal_set(runner)  # session "work", revision 1
    # plan-show on a goal with no plan says so, plainly.
    shown = runner.process_text(
        "```shell\nsession: work\naction: plan-show\n```", "/tmp")
    assert "has no plan" in shown.result_block
    # plan-set authors the document whole; it is a goal mutation.
    document = "# PLAN v1\n\nSome prose.\n\n- a bullet, not a step"
    set_outcome = runner.process_text(
        f"```shell\nsession: work\naction: plan-set\n{document}\n```",
        "/tmp")
    assert "plan set: # PLAN v1" in set_outcome.result_block
    assert "revision 2" in set_outcome.result_block
    # goal-show points at the document; plan-show returns it.
    shown = runner.process_text(
        "```shell\nsession: work\naction: goal-show\n```", "/tmp")
    assert "plan: # PLAN v1" in shown.result_block
    assert "a bullet, not a step" not in shown.result_block
    plan = runner.process_text(
        "```shell\nsession: work\naction: plan-show\n```", "/tmp")
    assert document in plan.result_block
    # Replacing is whole, and the guards ride along: the stale
    # expectation is rejected and the old document survives.
    stale = runner.process_text(
        "```shell\nsession: work\naction: plan-set\n"
        "expect-revision: 1\n# PLAN v2\n```", "/tmp")
    assert "stale revision: expected 1" in stale.result_block
    plan = runner.process_text(
        "```shell\nsession: work\naction: plan-show\n```", "/tmp")
    assert document in plan.result_block
    assert GoalLedger(tmp_path / "state").goal_for_session(
        "work")["revision"] == 2


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
    unrelated_before = {
        k: v for k, v in ledger.export_document(
            unrelated["goal_uid"]).items() if k != "exported_at"}

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
    for needle in ("PLAN v2", "revision: 2"):
        assert needle in shown.result_block, needle
    # The plan document comes back verbatim via plan-show — every
    # completion the old record asserted is in it (Joern, Gradle,
    # Helm all done inside PLAN v2).
    plan = runner.process_text(
        f"```shell\nsession: brand-new\naction: plan-show\n"
        f"goal: {NAV_UID}\n```", "/tmp")
    assert NAV_PLAN in plan.result_block
    for needle in ("Run Joern code-fact extraction",      # Joern done
                   "Gradle supplier discovery",           # Gradle done
                   "Helm supplier discovery"):            # Helm done
        assert needle in plan.result_block, needle

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
    # (its whole export document, field-for-field) throughout.
    assert executor.calls == []
    unrelated_after = {
        k: v for k, v in GoalLedger(state).export_document(
            unrelated["goal_uid"]).items() if k != "exported_at"}
    assert unrelated_after == unrelated_before
