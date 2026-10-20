"""The durable goal store — goal, plan and task as three things.

0.3.0 separates what 0.2.x fused into one record, following
Anthropic's Claude Code model, where the three are genuinely
different categories:

- GOAL — the anchor. The objective that frames the work: a
  harness-issued `goal_uid` (immutable, never derived from the
  session name or the goal text), a readable `human_id`, the
  verbatim goal text, a `status` (waiting / active / paused /
  complete / branched), a `revision` (starts at 1, +1 on EVERY
  mutation), `latest_result`, free-form `provenance`, timestamps.
  A goal holds NO plan, NO tasks, NO backlog — it only anchors
  them.

- TASK — the only tracked execution state. Each task is its own
  object belonging to a goal: a harness-issued `task_id`, the
  `goal_uid`, a title, a `status` (open / doing / done) and an
  integer `position`. There is no "current step" anywhere: which
  task is current is read off the task states (the one marked
  doing), never stored as a separate cursor that could disagree
  with them.

- PLAN — a document. At most one per goal: Markdown, authored
  whole, replaced whole. The harness stores and returns it
  verbatim and never parses steps, statuses or structure out of
  it. A plan's version identity ("PLAN v2") is the document's own
  content — its title line — not schema.

THE STORE IS FILES (Anthropic's literal mechanism: task objects
are JSON files in a folder; the folder is the store). Under
<state_dir>:

    goals/<goal_uid>/goal.json          the goal record
    goals/<goal_uid>/tasks/<id>.json    one file per task
    goals/<goal_uid>/plan.md            the plan document (if any)
    goals/<goal_uid>/actions.jsonl      applied action-ids (replay)
    bindings/<session_id>.json          session -> goal_uid

Writes go to a temp file in the same directory and land with
os.replace() (atomic rename). A mutation of one goal is guarded
by a per-goal lock directory so concurrent harness processes
serialize instead of corrupting; validation of a batch always
completes before any write, so a rejected batch changes nothing.

Guards, unchanged in spirit from the ledger era:

- Compare-and-swap: a mutation may carry an expected revision; if
  the record's current revision differs, the mutation is rejected
  (StaleRevision) and nothing changes — stale writers lose,
  loudly.
- Idempotency: applied action ids are remembered per goal (its
  actions.jsonl); session- and global-scoped ids ride a
  store-level actions.jsonl. A mutation carrying an
  already-applied action id is rejected (DuplicateAction) BEFORE
  anything changes, so a replayed clipboard payload can never
  double-apply.

Sessions hold only a binding (session id -> goal_uid). Losing a
session, a chat or a terminal loses nothing: `list_goals` /
`get` rediscover the record by uid.

Migrations (automatic, one-time, on first store open):

- A 0.2.x SQLite ledger (<state_dir>/goals.db) is read once and
  written out as the file layout: the plan dict is RENDERED to a
  Markdown document (thereafter user/Copilot-owned), tasks become
  task files, backlog entries become open tasks appended after
  them, bindings and applied action-ids carry over. The database
  is then renamed goals.db.migrated and never read again.
- The pre-0.2.0 per-session JSON files (<state_dir>/goals/
  <session>.json) migrate the same way they always did (fresh uid,
  session binding recorded, source renamed .migrated) — except
  sessions the SQLite migration already bound are skipped (the
  file is still renamed).
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

from .core import now_iso
from .sessions import _safe as _safe_session_name

EXPORT_SCHEMA = "clip-harness/goal-export@2"
LEGACY_EXPORT_SCHEMA = "clip-harness/goal-export@1"

GOAL_STATUSES = ("waiting", "active", "paused", "complete", "branched")
# Statuses a payload or the CLI may SET. `branched` is a state a
# record can be imported in (preserved field semantics), never set
# by hand — there are no branch operations.
SETTABLE_GOAL_STATUSES = ("waiting", "active", "paused", "complete")
TASK_STATUSES = ("open", "doing", "done")
# Legacy store goal statuses -> durable statuses (migration only).
_LEGACY_STATUS_MAP = {"active": "active", "paused": "paused",
                      "done": "complete"}

# The goal record's own fields — everything else about a goal
# (tasks, the plan) lives in its own objects beside it.
GOAL_FIELDS = ("goal_uid", "human_id", "goal", "status", "revision",
               "latest_result", "provenance", "created_at",
               "updated_at")

_LOCK_TIMEOUT_S = 15.0

_UNSET = object()  # "leave the plan alone" marker for record writes


class GoalStoreError(ValueError):
    """A durable-store rejection (import validation, unknown uid…)."""


class StaleRevision(GoalStoreError):
    """Compare-and-swap failure: expected != current revision."""

    def __init__(self, expected: int, actual: int):
        super().__init__(
            f"stale revision: expected {expected}, current is {actual}")
        self.expected = expected
        self.actual = actual


class DuplicateAction(GoalStoreError):
    """Idempotency rejection: this action id was already applied."""

    def __init__(self, action_id: str, outcome: str):
        super().__init__(f"duplicate action-id {action_id}: {outcome}")
        self.action_id = action_id
        self.outcome = outcome


def new_goal_uid() -> str:
    return str(uuid.uuid4())


def new_task_id() -> str:
    return "t-" + uuid.uuid4().hex[:8]


def slugify(text: str, fallback: str = "goal") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48].strip("-") or fallback


# -- file primitives -----------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the same directory + os.replace():
    a reader never sees a half-written file, and a crash leaves
    either the old bytes or the new ones, never a blend."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _append_jsonl(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn final append must not brick the log
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


class _DirLock:
    """A cross-process mutex as a directory (os.mkdir is atomic):
    the stdlib-only lock for a store with no database engine.
    Waiters retry for a bounded window (~15s); on timeout the lock
    is reported LOUDLY rather than broken — a lock another process
    legitimately holds must never be stolen, and a stale lock left
    by a crashed process is a human-visible error naming the path
    to remove, not a silent corruption vector."""

    def __init__(self, path: Path, timeout: float = _LOCK_TIMEOUT_S):
        self.path = path
        self.timeout = timeout

    def __enter__(self) -> "_DirLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        delay = 0.02
        while True:
            try:
                os.mkdir(self.path)
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise GoalStoreError(
                        f"timed out after {self.timeout:g}s waiting "
                        f"for the store lock at {self.path} — another "
                        f"clip-harness process is mutating this "
                        f"goal, or a crashed process left the lock "
                        f"behind (remove the directory if no harness "
                        f"is running)")
                time.sleep(delay)
                delay = min(delay * 2, 0.25)

    def __exit__(self, *exc) -> None:
        try:
            os.rmdir(self.path)
        except OSError:
            pass


def _is_goal_scope(scope: str) -> bool:
    """An idempotency scope names a goal (its uid) unless it is the
    global scope ("") or a session scope ("session:<id>")."""
    return bool(scope) and not scope.startswith("session:")


class GoalLedger:
    """The file-backed durable goal store (see module docstring).

    The name survives from the SQLite era: it is still the one
    handle through which runner, session view and CLI read and
    mutate durable goals — only the substrate changed.
    """

    def __init__(self, state_dir: Path | str):
        self.state_dir = Path(state_dir).expanduser()
        self.goals_dir = self.state_dir / "goals"
        self.bindings_dir = self.state_dir / "bindings"
        self.migration_notes: list[dict] = []
        self._migrate()

    # -- paths ---------------------------------------------------------------

    def _goal_dir(self, uid: str) -> Path:
        return self.goals_dir / uid

    def _goal_file(self, uid: str) -> Path:
        return self._goal_dir(uid) / "goal.json"

    def _plan_file(self, uid: str) -> Path:
        return self._goal_dir(uid) / "plan.md"

    def _goal_actions_file(self, uid: str) -> Path:
        return self._goal_dir(uid) / "actions.jsonl"

    def _store_actions_file(self) -> Path:
        return self.state_dir / "actions.jsonl"

    def _binding_file(self, session_id: str) -> Path:
        return self.bindings_dir / f"{_safe_session_name(session_id)}.json"

    def _store_lock(self) -> _DirLock:
        return _DirLock(self.state_dir / ".store.lock")

    def _goal_lock(self, uid: str) -> _DirLock:
        return _DirLock(self._goal_dir(uid) / ".lockdir")

    # -- reads -----------------------------------------------------------------

    def _read_goal_fields(self, uid: str) -> dict | None:
        data = _read_json(self._goal_file(uid))
        if data is None or not data.get("goal_uid"):
            return None
        return {field: data.get(field) for field in GOAL_FIELDS}

    def _read_tasks(self, uid: str) -> list[dict]:
        tasks_dir = self._goal_dir(uid) / "tasks"
        tasks: list[dict] = []
        if tasks_dir.exists():
            for path in sorted(tasks_dir.glob("*.json")):
                data = _read_json(path)
                if data is None or "title" not in data:
                    continue
                tasks.append({
                    "task_id": data.get("task_id", path.stem),
                    "goal_uid": data.get("goal_uid", uid),
                    "title": data["title"],
                    "status": data.get("status", "open"),
                    "position": data.get("position", 0),
                })
        tasks.sort(key=lambda t: (t["position"], t["task_id"]))
        return tasks

    def _read_plan(self, uid: str) -> str | None:
        path = self._plan_file(uid)
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8")
        # The file carries exactly one trailing newline (the write
        # side guarantees it); the document itself is everything
        # before it.
        return text[:-1] if text.endswith("\n") else text

    def get(self, uid: str) -> dict | None:
        """The assembled goal view: the goal record's fields, plus
        `tasks` (its task objects, in position order) and `plan`
        (the plan document verbatim, or None)."""
        fields = self._read_goal_fields(uid)
        if fields is None:
            return None
        view = dict(fields)
        view["tasks"] = self._read_tasks(uid)
        view["plan"] = self._read_plan(uid)
        return view

    def list_goals(self) -> list[dict]:
        if not self.goals_dir.exists():
            return []
        views = []
        for child in sorted(self.goals_dir.iterdir()):
            # Goal folders only: directories holding a goal.json.
            # (Top-level *.json files are the legacy session store,
            # handled by migration — never goals.)
            if child.is_dir() and (child / "goal.json").exists():
                view = self.get(child.name)
                if view is not None:
                    views.append(view)
        views.sort(key=lambda v: (v["created_at"] or "",
                                  v["goal_uid"]))
        return views

    def goal_for_session(self, session_id: str) -> dict | None:
        uid = self.bound_uid(session_id)
        return self.get(uid) if uid else None

    def bound_uid(self, session_id: str) -> str | None:
        data = _read_json(self._binding_file(session_id))
        return data.get("goal_uid") if data else None

    # -- bindings ----------------------------------------------------------------

    def _write_binding(self, session_id: str, uid: str,
                       bound_at: str | None = None) -> None:
        _atomic_write(self._binding_file(session_id), json.dumps(
            {"goal_uid": uid, "bound_at": bound_at or now_iso()},
            indent=2, ensure_ascii=False) + "\n")

    def bind_session(self, session_id: str, uid: str) -> None:
        if self._read_goal_fields(uid) is None:
            raise GoalStoreError(f"no such goal: {uid}")
        self._write_binding(session_id, uid)

    # -- writes ------------------------------------------------------------------

    def _write_record(self, uid: str, fields: dict, tasks: list[dict],
                      plan=_UNSET) -> None:
        """Persist one goal: its record file, its task files (the
        given set replaces whatever is on disk), and — only when
        `plan` is given — its plan document (a string writes it,
        None removes it)."""
        goal_dir = self._goal_dir(uid)
        goal_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(self._goal_file(uid), json.dumps(
            fields, indent=2, ensure_ascii=False) + "\n")
        tasks_dir = goal_dir / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        wanted = {task["task_id"]: task for task in tasks}
        for existing in tasks_dir.glob("*.json"):
            if existing.stem not in wanted:
                existing.unlink()
        for task in tasks:
            _atomic_write(tasks_dir / f"{task['task_id']}.json",
                          json.dumps(task, indent=2,
                                     ensure_ascii=False) + "\n")
        if plan is not _UNSET:
            plan_path = self._plan_file(uid)
            if plan is None:
                if plan_path.exists():
                    plan_path.unlink()
            else:
                _atomic_write(plan_path,
                              plan.rstrip("\n") + "\n")

    # -- idempotency scopes -------------------------------------------------
    #
    # An action id is checked against every scope the payload could
    # have been recorded under (its goal, its session, the global
    # scope) and recorded under the MOST SPECIFIC one that applies.
    # Ids are model-issued uuids, unique per logical action, so the
    # scopes are storage hygiene, not a security boundary: a replay
    # is caught whichever door it comes back through. Goal scopes
    # live in the goal's own actions.jsonl; session/global scopes
    # share the store-level actions.jsonl (their entries carry the
    # scope with them).

    def _outcome_in_scope(self, scope: str,
                            action_id: str) -> str | None:
        if _is_goal_scope(scope):
            entries = _read_jsonl(self._goal_actions_file(scope))
            for entry in entries:
                if entry.get("action_id") == action_id:
                    return entry.get("outcome", "")
            return None
        for entry in _read_jsonl(self._store_actions_file()):
            if entry.get("scope") == scope \
                    and entry.get("action_id") == action_id:
                return entry.get("outcome", "")
        return None

    def _check_action(self, scopes: list[str],
                      action_id: str | None) -> None:
        if not action_id:
            return
        for scope in scopes:
            outcome = self._outcome_in_scope(scope, action_id)
            if outcome is not None:
                raise DuplicateAction(action_id, outcome)

    def _record_action(self, scope: str, action_id: str | None,
                       outcome: str) -> None:
        if not action_id:
            return
        entry = {"action_id": action_id, "outcome": outcome,
                 "applied_at": now_iso()}
        if _is_goal_scope(scope) and self._goal_file(scope).exists():
            _append_jsonl(self._goal_actions_file(scope), entry)
        else:
            entry["scope"] = scope
            _append_jsonl(self._store_actions_file(), entry)

    def action_outcome(self, scopes: list[str],
                       action_id: str) -> str | None:
        """The recorded outcome for an action id, or None — the
        read-only pre-check the runner uses before it dispatches."""
        for scope in scopes:
            outcome = self._outcome_in_scope(scope, action_id)
            if outcome is not None:
                return outcome
        return None

    def record_action(self, scope: str, action_id: str,
                      outcome: str) -> None:
        """Remember an applied action id under a scope — for
        applies, like plain runs and goal creation, that are not
        record mutations and so don't ride _mutate."""
        self._record_action(scope, action_id, outcome)

    # -- creation -------------------------------------------------------------

    def create_goal(self, goal_text: str, *, human_id: str = "",
                    status: str = "active",
                    provenance: dict | None = None,
                    session_id: str | None = None,
                    action_id: str | None = None,
                    action_scopes: list[str] | None = None,
                    outcome: str = "") -> dict:
        """Create a durable goal (harness issues the uid, revision
        1) and optionally bind a session to it — including the
        duplicate-action check and recording."""
        if status not in GOAL_STATUSES:
            raise GoalStoreError(
                f"goal status must be one of {GOAL_STATUSES}")
        self._check_action(action_scopes or [], action_id)
        now = now_iso()
        fields = {
            "goal_uid": new_goal_uid(),
            "human_id": human_id or slugify(goal_text)
            or slugify(session_id or "", "goal"),
            "goal": goal_text, "status": status, "revision": 1,
            "latest_result": "", "provenance": provenance or {},
            "created_at": now, "updated_at": now,
        }
        uid = fields["goal_uid"]
        self._write_record(uid, fields, [])
        if session_id is not None:
            self._write_binding(session_id, uid, now)
        self._record_action(uid, action_id,
                            outcome or f"goal set: {goal_text}")
        return self.get(uid)

    def ensure_session_goal(self, session_id: str) -> dict:
        """The session's bound goal, created (empty text) and bound
        when the session has none — the first goal/task mutation of
        a session materialises its durable goal object."""
        record = self.goal_for_session(session_id)
        if record is not None:
            return record
        with self._store_lock():
            record = self.goal_for_session(session_id)
            if record is not None:
                return record
            return self.create_goal(
                "", provenance={"created_for_session": session_id},
                session_id=session_id)

    # -- mutation engine ------------------------------------------------------
    #
    # Every mutation of an existing goal runs through _mutate under
    # the goal's lock: resolve the view, check the action id
    # (duplicate), check the expected revision (compare-and-swap),
    # apply the change function, bump revision + updated_at, write
    # the record and its task files, record the action id — or
    # change nothing at all.

    def _mutate(self, uid: str, change, *,
                expect_revision: int | None = None,
                action_id: str | None = None,
                action_scopes: list[str] | None = None,
                record_scope: str | None = None,
                outcome: str = ""):
        with self._goal_lock(uid):
            view = self.get(uid)
            if view is None:
                raise GoalStoreError(f"no such goal: {uid}")
            self._check_action(action_scopes or [uid], action_id)
            if expect_revision is not None \
                    and view["revision"] != expect_revision:
                raise StaleRevision(expect_revision, view["revision"])
            result = change(view)
            view["revision"] += 1
            view["updated_at"] = now_iso()
            plan = view.pop("_plan", _UNSET)
            fields = {field: view[field] for field in GOAL_FIELDS}
            self._write_record(uid, fields, view["tasks"], plan)
            self._record_action(record_scope or uid, action_id,
                                outcome)
            return result, self.get(uid)

    def set_goal_text(self, uid: str, text: str, *,
                      expect_revision: int | None = None,
                      action_id: str | None = None,
                      action_scopes: list[str] | None = None,
                      outcome: str = "") -> dict:
        def change(view):
            view["goal"] = text
            if not view["human_id"]:
                view["human_id"] = slugify(text)
            return view
        _, record = self._mutate(
            uid, change, expect_revision=expect_revision,
            action_id=action_id, action_scopes=action_scopes,
            outcome=outcome or f"goal set: {text}")
        return record

    def set_status(self, uid: str, status: str, *,
                   expect_revision: int | None = None,
                   action_id: str | None = None,
                   action_scopes: list[str] | None = None,
                   outcome: str = "") -> dict:
        if status not in SETTABLE_GOAL_STATUSES:
            raise GoalStoreError(
                f"goal status must be one of {SETTABLE_GOAL_STATUSES}")

        def change(view):
            view["status"] = status
            return view
        _, record = self._mutate(
            uid, change, expect_revision=expect_revision,
            action_id=action_id, action_scopes=action_scopes,
            outcome=outcome or f"goal status: {status}")
        return record

    def set_plan(self, uid: str, document: str, *,
                 expect_revision: int | None = None,
                 action_id: str | None = None,
                 action_scopes: list[str] | None = None,
                 outcome: str = "") -> dict:
        """Replace the goal's plan document, whole — a goal
        mutation like any other (revision +1, same guards). The
        document is stored verbatim; nothing is parsed out of it.
        There is no plan deletion: an empty document is refused,
        never stored."""
        if not document or not document.strip():
            raise GoalStoreError(
                "plan document is empty — plan-set replaces the "
                "goal's plan with the payload body; send the full "
                "Markdown document")

        def change(view):
            view["_plan"] = document
            return view
        _, record = self._mutate(
            uid, change, expect_revision=expect_revision,
            action_id=action_id, action_scopes=action_scopes,
            outcome=outcome or "plan set")
        return record

    def get_plan(self, uid: str) -> str | None:
        """The goal's plan document, verbatim — or None when the
        goal has none (or does not exist)."""
        return self._read_plan(uid)

    def add_tasks(self, uid: str, titles: list[str], *,
                  status: str = "open",
                  expect_revision: int | None = None,
                  action_id: str | None = None,
                  action_scopes: list[str] | None = None,
                  outcome: str = "") -> tuple[list[dict], dict]:
        """Append tasks (positions after the current maximum);
        returns (added, record)."""
        if status not in TASK_STATUSES:
            raise GoalStoreError(
                f"task status must be one of {TASK_STATUSES}")

        def change(view):
            base = max((t["position"] for t in view["tasks"]),
                       default=0)
            added = []
            for offset, title in enumerate(titles):
                task = {"task_id": new_task_id(), "goal_uid": uid,
                        "title": title, "status": status,
                        "position": base + offset + 1}
                view["tasks"].append(task)
                added.append(task)
            return added
        return self._mutate(
            uid, change, expect_revision=expect_revision,
            action_id=action_id, action_scopes=action_scopes,
            outcome=outcome or f"tasks added: {len(titles)}")

    def set_task_status(self, uid: str, title: str, status: str, *,
                        expect_revision: int | None = None,
                        action_id: str | None = None,
                        action_scopes: list[str] | None = None,
                        outcome: str = "") -> tuple[dict | None, dict]:
        """One task's status (the terminal CLI's single-task form).
        Returns (task|None, record); an unknown title is NOT a
        mutation — the lookup happens before _mutate is entered, so
        no revision is bumped for a miss."""
        if status not in TASK_STATUSES:
            raise GoalStoreError(
                f"task status must be one of {TASK_STATUSES}")
        record = self.get(uid)
        if record is None:
            raise GoalStoreError(f"no such goal: {uid}")
        if not any(t["title"] == title for t in record["tasks"]):
            return None, record

        def change(view):
            for task in view["tasks"]:
                if task["title"] == title:
                    task["status"] = status
                    return task
            return None
        return self._mutate(
            uid, change, expect_revision=expect_revision,
            action_id=action_id, action_scopes=action_scopes,
            outcome=outcome or f"task {status}: {title}")

    def apply_task_transitions(
            self, uid: str, transitions: list[dict], *,
            expect_revision: int | None = None,
            action_id: str | None = None,
            action_scopes: list[str] | None = None,
            outcome: str = "") -> tuple[list[dict], list[dict], dict]:
        """The atomic batch: validate the SET against the goal's
        tasks, then apply — or not at all. Duplicate/conflicting/
        unknown titles return as errors with NOTHING applied and NO
        revision bump; a rejected batch's action id is deliberately
        NOT recorded either (a fixed re-send carries a new action
        id, and the batch's own no-op semantics already make
        identical re-sends safe). Returns (applied, errors,
        record-after)."""
        for transition in transitions:
            if transition["status"] not in TASK_STATUSES:
                raise GoalStoreError(
                    f"task status must be one of {TASK_STATUSES}")
        with self._goal_lock(uid):
            view = self.get(uid)
            if view is None:
                raise GoalStoreError(f"no such goal: {uid}")
            self._check_action(action_scopes or [uid], action_id)
            if expect_revision is not None \
                    and view["revision"] != expect_revision:
                raise StaleRevision(expect_revision, view["revision"])
            applied, errors = _validate_transitions(
                view["tasks"], transitions)
            if errors:
                return [], errors, view
            by_id = {t["task_id"]: t for t in view["tasks"]}
            for entry in applied:
                by_id[entry["task_id"]]["status"] = entry["to"]
            view["revision"] += 1
            view["updated_at"] = now_iso()
            fields = {field: view[field] for field in GOAL_FIELDS}
            self._write_record(uid, fields, view["tasks"])
            self._record_action(uid, action_id,
                                outcome or "task-update applied")
            # The task_id was plumbing for the apply above; the
            # public entry shape is {title, from, to, noop}.
            for entry in applied:
                entry.pop("task_id", None)
            return applied, [], self.get(uid)

    # -- export / import -------------------------------------------------------
    #
    # Backup/restore only. The @2 document is the goal's own
    # fields, its tasks as {title, status} in position order, and
    # its plan as a Markdown string (or null). Applied action
    # history is NOT exported: an import starts a fresh replay log
    # in the new store.

    def _export_bundle(self, view: dict) -> dict:
        return {
            "goal": {field: view[field] for field in GOAL_FIELDS},
            "tasks": [{"title": t["title"], "status": t["status"]}
                      for t in view["tasks"]],
            "plan": view["plan"],
        }

    def export_document(self, uid: str) -> dict:
        view = self.get(uid)
        if view is None:
            raise GoalStoreError(f"no such goal: {uid}")
        return {"format": EXPORT_SCHEMA, "exported_at": now_iso(),
                **self._export_bundle(view)}

    def export_all_document(self) -> dict:
        return {"format": EXPORT_SCHEMA, "exported_at": now_iso(),
                "goals": [self._export_bundle(view)
                          for view in self.list_goals()]}

    def import_document(self, document: dict) -> list[dict]:
        """Import an export document (a single goal bundle, or a
        `goals` list of them). Goal fields — uid, revision, status,
        provenance, timestamps — are preserved EXACTLY, tasks are
        recreated as fresh task objects in position order, and the
        plan document is stored verbatim. An existing goal_uid is
        refused by name, never overwritten; every bundle is
        validated (including all refusal checks) before any file
        is written, so a refused document imports nothing."""
        bundles = _validate_export(document)
        for fields, _tasks, _plan in bundles:
            if self._goal_file(fields["goal_uid"]).exists():
                raise GoalStoreError(
                    f"goal {fields['goal_uid']} already exists — "
                    f"import refuses to overwrite it")
        imported = []
        for fields, tasks, plan in bundles:
            uid = fields["goal_uid"]
            task_objects = [
                {"task_id": new_task_id(), "goal_uid": uid,
                 "title": task["title"], "status": task["status"],
                 "position": index + 1}
                for index, task in enumerate(tasks)]
            self._write_record(uid, fields, task_objects,
                               plan if plan is not None else _UNSET)
            imported.append(self.get(uid))
        return imported

    # -- migrations --------------------------------------------------------------
    #
    # Both migrations run under the store lock on first open, so two
    # processes constructing the store together cannot double-import
    # (the file-era successor of the case-33 startup races): the
    # loser waits, then finds nothing left to migrate.

    def _migrate(self) -> None:
        db_path = self.state_dir / "goals.db"
        has_legacy = self.goals_dir.exists() and any(
            self.goals_dir.glob("*.json"))
        if not db_path.exists() and not has_legacy:
            return
        with self._store_lock():
            if db_path.exists():
                self._migrate_sqlite(db_path)
            if self.goals_dir.exists():
                for path in sorted(self.goals_dir.glob("*.json")):
                    self._migrate_legacy_file(path)

    def _migrate_sqlite(self, db_path: Path) -> None:
        """0.2.x SQLite ledger -> the file layout, once.

        The fused record separates back into the three concepts:
        the plan dict is RENDERED as a Markdown document (title
        line from its revision label, one "N. [status] title" line
        per step) — a rendering, whose content is user/Copilot-
        owned from then on; tasks become task files; backlog
        entries become open tasks appended after them. Bindings
        and applied action-ids carry over to their file homes. The
        database (and any -wal/-shm beside it) is renamed
        .migrated and never read again."""
        import sqlite3  # migration only — the store itself is files

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            goal_rows = (conn.execute("SELECT * FROM goals").fetchall()
                         if "goals" in tables else [])
            binding_rows = (
                conn.execute("SELECT * FROM session_bindings").fetchall()
                if "session_bindings" in tables else [])
            action_rows = (
                conn.execute(
                    "SELECT * FROM applied_actions "
                    "ORDER BY applied_at").fetchall()
                if "applied_actions" in tables else [])
        finally:
            conn.close()
        for row in goal_rows:
            fields, tasks, plan = _sqlite_record_to_bundle(row)
            self._write_record(fields["goal_uid"], fields, tasks,
                               plan if plan is not None else _UNSET)
            self.migration_notes.append({
                "source": "sqlite ledger (0.2.x)",
                "goal_uid": fields["goal_uid"],
                "goal": fields["goal"],
                "tasks": len(tasks),
                "plan": plan is not None})
        for row in binding_rows:
            self._write_binding(row["session_id"], row["goal_uid"],
                                row["bound_at"])
        for row in action_rows:
            entry = {"action_id": row["action_id"],
                     "outcome": row["outcome"],
                     "applied_at": row["applied_at"]}
            scope = row["scope"]
            if _is_goal_scope(scope) \
                    and self._goal_file(scope).exists():
                _append_jsonl(self._goal_actions_file(scope), entry)
            else:
                entry["scope"] = scope
                _append_jsonl(self._store_actions_file(), entry)
        for suffix in ("", "-wal", "-shm"):
            sibling = self.state_dir / f"goals.db{suffix}"
            if sibling.exists():
                sibling.rename(sibling.with_name(
                    sibling.name + ".migrated"))

    def _migrate_legacy_file(self, path: Path) -> None:
        """One pre-0.2.0 per-session JSON file -> a durable goal.

        Same semantics as the original migration (fresh uid, the
        session — the filename stem; the legacy safe-name transform
        is exact for ordinary ids — is bound, legacy `done` ->
        `complete`), retargeted at the file layout. A session the
        SQLite migration already bound is skipped: its legacy file
        would only duplicate the goal. The source file is renamed
        `<name>.migrated` either way — preserved, never re-read."""
        session_id = path.stem
        try:
            if self.bound_uid(session_id) is None:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    note = self._import_legacy_data(session_id, data)
                    if note is not None:
                        self.migration_notes.append(note)
        finally:
            path.rename(path.with_suffix(path.suffix + ".migrated"))

    def _import_legacy_data(self, session_id: str,
                            data: dict) -> dict | None:
        goal = data.get("goal")
        tasks = [t for t in data.get("tasks", [])
                 if isinstance(t, dict) and t.get("title")]
        if goal is None and not tasks:
            return None
        now = now_iso()
        goal_text = str(goal.get("title", "")) if goal else ""
        status = _LEGACY_STATUS_MAP.get(
            str(goal.get("status", "active")) if goal else "active",
            "active")
        uid = new_goal_uid()
        fields = {
            "goal_uid": uid,
            "human_id": slugify(goal_text) if goal_text
            else slugify(session_id),
            "goal": goal_text, "status": status, "revision": 1,
            "latest_result": "",
            "provenance": {"migrated_from": "legacy session store",
                           "session": session_id,
                           "source_file": f"{session_id}.json"},
            "created_at": now, "updated_at": now,
        }
        task_objects = [
            {"task_id": new_task_id(), "goal_uid": uid,
             "title": str(t["title"]),
             "status": t["status"]
             if t.get("status") in TASK_STATUSES else "open",
             "position": index + 1}
            for index, t in enumerate(tasks)]
        self._write_record(uid, fields, task_objects)
        self._write_binding(session_id, uid, now)
        return {"session": session_id, "goal_uid": uid,
                "goal": goal_text, "tasks": len(task_objects)}


def _sqlite_record_to_bundle(row) -> tuple[dict, list[dict],
                                           str | None]:
    """One fused 0.2.x ledger row -> (goal fields, task objects,
    plan document|None) in the 0.3.0 model."""
    def decoded(column):
        value = row[column]
        return json.loads(value) if value is not None else None

    fields = {
        "goal_uid": row["goal_uid"],
        "human_id": row["human_id"],
        "goal": row["goal"],
        "status": row["status"],
        "revision": row["revision"],
        "latest_result": row["latest_result"],
        "provenance": decoded("provenance") or {},
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    uid = fields["goal_uid"]
    task_objects = []
    position = 0
    for task in decoded("tasks") or []:
        position += 1
        task_objects.append({
            "task_id": new_task_id(), "goal_uid": uid,
            "title": task["title"], "status": task["status"],
            "position": position})
    for entry in decoded("backlog") or []:
        # The backlog field is deleted in the new model; its
        # entries survive as open tasks after the record's tasks.
        position += 1
        task_objects.append({
            "task_id": new_task_id(), "goal_uid": uid,
            "title": entry, "status": "open", "position": position})
    plan_doc = None
    plan = decoded("plan")
    if plan is not None:
        label = plan.get("revision_label") or "Plan"
        lines = [f"# {label}", ""]
        for step in plan.get("steps", []):
            lines.append(f"{step.get('n', '')}. "
                         f"[{step.get('status', 'open')}] "
                         f"{step.get('title', '')}")
        plan_doc = "\n".join(lines)
    return fields, task_objects, plan_doc


def _validate_export(document) -> list[tuple[dict, list[dict],
                                             str | None]]:
    """Strict shape check of an export document; returns
    (goal fields, tasks, plan) bundles."""
    if not isinstance(document, dict):
        raise GoalStoreError("export document must be a JSON object")
    if document.get("schema") == LEGACY_EXPORT_SCHEMA \
            or "schema" in document:
        raise GoalStoreError(
            "this is a clip-harness/goal-export@1 document — the "
            "@1 format is retired and no longer imported (it "
            "carried the fused 0.2.x goal record; goals are now "
            "goal/plan/task, exported as "
            "clip-harness/goal-export@2). Re-export the goal with "
            "clip-harness 0.3.0 and import that document instead")
    if document.get("format") != EXPORT_SCHEMA:
        raise GoalStoreError(
            f"export document format must be {EXPORT_SCHEMA!r}, "
            f"got {document.get('format')!r}")
    raw_bundles = []
    if isinstance(document.get("goal"), dict):
        raw_bundles.append(document)
    if isinstance(document.get("goals"), list):
        raw_bundles.extend(document["goals"])
    if not raw_bundles:
        raise GoalStoreError(
            "export document carries no goal ('goal' or 'goals')")
    return [_validate_bundle(raw) for raw in raw_bundles]


def _validate_bundle(raw) -> tuple[dict, list[dict], str | None]:
    if not isinstance(raw, dict) \
            or not isinstance(raw.get("goal"), dict):
        raise GoalStoreError(
            "each exported goal must be an object with a 'goal' "
            "record, a 'tasks' list and a 'plan'")
    goal = raw["goal"]
    uid = goal.get("goal_uid")
    if not isinstance(uid, str) or not uid:
        raise GoalStoreError("goal record has no goal_uid")
    if goal.get("status") not in GOAL_STATUSES:
        raise GoalStoreError(
            f"goal {uid}: status must be one of {GOAL_STATUSES}, "
            f"got {goal.get('status')!r}")
    revision = goal.get("revision")
    if not isinstance(revision, int) or revision < 1:
        raise GoalStoreError(
            f"goal {uid}: revision must be a positive integer")
    if not isinstance(goal.get("goal"), str):
        raise GoalStoreError(f"goal {uid}: goal text must be a string")
    now = now_iso()
    fields = {
        "goal_uid": uid,
        "human_id": goal.get("human_id") or slugify(goal["goal"]),
        "goal": goal["goal"],
        "status": goal["status"],
        "revision": revision,
        "latest_result": goal.get("latest_result") or "",
        "provenance": goal.get("provenance")
        if isinstance(goal.get("provenance"), dict) else {},
        "created_at": goal.get("created_at") or now,
        "updated_at": goal.get("updated_at") or now,
    }
    tasks = raw.get("tasks") or []
    if not isinstance(tasks, list) or not all(
            isinstance(t, dict) and isinstance(t.get("title"), str)
            and t.get("status") in TASK_STATUSES for t in tasks):
        raise GoalStoreError(
            f"goal {uid}: tasks must be a list of {{title, status}} "
            f"with status in {TASK_STATUSES}")
    plan = raw.get("plan")
    if plan is not None and not isinstance(plan, str):
        raise GoalStoreError(
            f"goal {uid}: plan must be a Markdown string or null")
    return fields, [{"title": t["title"], "status": t["status"]}
                    for t in tasks], plan


# -- rendering (shared by the payload actions and the CLI) ---------------------


def _plan_title(plan_text: str | None) -> str | None:
    """The plan document's first line — its title, where its
    version identity lives ("PLAN v2" is content, not schema)."""
    if not plan_text:
        return None
    lines = plan_text.strip().splitlines()
    return lines[0].strip() if lines else None


def render_section(record: dict | None) -> str:
    """The sticky footer section projected from a durable record.

    Fixed shape, fixed position (always the last section of a bound
    result block): the [goal] line (goal text, status, short uid)
    and the [open tasks] line (the goal's non-done tasks, in
    position order — an in-progress task shows as [doing] there;
    there is no [step] line, because there is no stored current
    step). A goal-less session renders the legacy "(none set)"
    shape."""
    if record is None or not record["goal"]:
        lines = ["[goal] (none set)"]
        tasks = record["tasks"] if record else []
    else:
        lines = [f"[goal] {record['goal']} [{record['status']}] "
                 f"({record['goal_uid'][:8]})"]
        tasks = record["tasks"]
    open_tasks = [t for t in tasks if t["status"] != "done"]
    if open_tasks:
        listing = " · ".join(
            f"[{t['status']}] {t['title']}" for t in open_tasks)
        lines.append(f"[open tasks] {listing}")
    else:
        lines.append("[open tasks] none")
    return "\n".join(lines)


def render_goal_list(records: list[dict]) -> str:
    if not records:
        return "durable goals: none"
    lines = [f"durable goals ({len(records)}):"]
    for record in records:
        lines.append(
            f"  {record['goal_uid']} · {record['human_id']} · "
            f"{record['status']} · rev {record['revision']}")
    return "\n".join(lines)


def render_goal_show(record: dict | None,
                     uid_hint: str = "") -> str:
    if record is None:
        return f"no such goal: {uid_hint}" if uid_hint \
            else "no goal bound to this session"
    lines = [f"goal: {record['goal']}" if record["goal"]
             else "goal: (no goal text set)",
             f"uid: {record['goal_uid']} · id: {record['human_id']} · "
             f"status: {record['status']} · "
             f"revision: {record['revision']}"]
    title = _plan_title(record.get("plan"))
    if title is not None:
        lines.append(f"plan: {title} "
                     f"(full document: 'action: plan-show')")
    else:
        lines.append("plan: none")
    if record["tasks"]:
        lines.append("tasks:")
        for task in record["tasks"]:
            lines.append(f"  [{task['status']}] {task['title']}")
    else:
        lines.append("tasks: none")
    if record["latest_result"]:
        lines.append(f"latest result: {record['latest_result']}")
    if record["provenance"]:
        lines.append("provenance: " + json.dumps(
            record["provenance"], ensure_ascii=False, sort_keys=True))
    lines.append(f"created: {record['created_at']} · "
                 f"updated: {record['updated_at']}")
    return "\n".join(lines)


def render_plan_show(record: dict | None,
                     uid_hint: str = "") -> str:
    """The plan document, verbatim — or the honest statement that
    there is nothing to show."""
    if record is None:
        return f"no such goal: {uid_hint}" if uid_hint \
            else "no goal bound to this session"
    plan = record.get("plan")
    if plan is None:
        return (f"goal {record['goal_uid']} has no plan — author "
                f"one with 'action: plan-set' (the body is the "
                f"whole Markdown document)")
    return plan


def _validate_transitions(tasks: list[dict],
                          transitions: list[dict]
                          ) -> tuple[list[dict], list[dict]]:
    """Judge the SET, Anthropic-style: every title is resolved
    against the current tasks first; duplicates (same title, same
    status twice), conflicts (same title, different statuses) and
    unknown titles are named, and nothing applies. No-op re-sets
    (already in the target status) are valid and reported as such.
    Returns (applied, errors) — applied entries carry the task_id
    so the caller mutates objects, not titles."""
    by_title: dict[str, dict] = {}
    for task in tasks:
        by_title.setdefault(task["title"], task)
    counts: dict[str, int] = {}
    for transition in transitions:
        title = transition["title"]
        counts[title] = counts.get(title, 0) + 1
    errors: list[dict] = []
    flagged: set[str] = set()
    for transition in transitions:
        title = transition["title"]
        if title in flagged:
            continue
        if counts[title] > 1:
            statuses = {t["status"] for t in transitions
                        if t["title"] == title}
            problem = "conflicting" if len(statuses) > 1 \
                else "duplicate"
            errors.append({"title": title, "problem": problem})
            flagged.add(title)
        elif title not in by_title:
            errors.append({"title": title, "problem": "unknown"})
            flagged.add(title)
    if errors:
        return [], errors
    applied: list[dict] = []
    for transition in transitions:
        task = by_title[transition["title"]]
        old = task["status"]
        applied.append({"task_id": task["task_id"],
                        "title": transition["title"],
                        "from": old, "to": transition["status"],
                        "noop": old == transition["status"]})
    return applied, []


