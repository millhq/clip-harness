"""The durable goal store — goals as first-class durable objects.

THE GAP THIS CLOSES (work Copilot's analysis, accepted by Ali): the
old M365 harness's SQLite ledger stayed the system of record for
long-running work because clip-harness goals were session-scoped
JSON files — they died with the session, the chat, the terminal.
Anthropic's converged pattern is the opposite: task/goal objects are
durable, carry stable ids, and are REFERENCED by sessions, never
owned by them. This module is that pattern in clip-harness:

- One SQLite database at <state_dir>/goals.db (Python's stdlib
  sqlite3 — the clean tool for a transactional ledger; the harness
  still has zero third-party dependencies), WAL mode, ONE
  transaction per mutation (BEGIN IMMEDIATE … COMMIT).

- A goal record: `goal_uid` (harness-issued uuid4, immutable, never
  derived from the session name or the goal text), `human_id` (a
  short readable slug), the verbatim `goal` text, `status`
  (waiting / active / paused / complete / branched), `revision`
  (starts at 1, +1 on EVERY mutation), an optional `plan`
  ({revision_label, steps: [{n, title, status}], current_step}),
  `tasks` ([{title, status}]), `backlog` (strings),
  `latest_result`, `parent_uid` / `fork_revision` (PRESERVED fields
  only — there are deliberately no branch operations), free-form
  `provenance`, and created/updated timestamps.

- Compare-and-swap: a mutation may carry an expected revision; if
  the record's current revision differs, the mutation is rejected
  (StaleRevision) and nothing changes — stale writers lose, loudly.

- Idempotency: applied action ids are remembered in
  `applied_actions`, scoped per goal when a goal is involved, then
  per session, then global. A mutation carrying an already-applied
  action id is rejected (DuplicateAction) BEFORE anything changes,
  so a replayed clipboard payload can never double-apply.

- Sessions hold only a binding (session id -> goal_uid) in
  `session_bindings`. Losing a session, a chat or a terminal loses
  nothing: `list_goals` / `get` rediscover the record by uid.

Migration: the legacy per-session JSON store (<state_dir>/goals/
<session>.json) is imported ONCE, on first open under this code —
each legacy session state becomes a durable goal (fresh uid), the
session binding is recorded, and the legacy file is renamed with a
`.migrated` suffix (preserved on disk, never read again).
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from pathlib import Path

from .core import now_iso

SCHEMA_VERSION = "1"
EXPORT_SCHEMA = "clip-harness/goal-export@1"

GOAL_STATUSES = ("waiting", "active", "paused", "complete", "branched")
# Statuses a payload or the CLI may SET. `branched` is a state a
# record can be imported in (preserved field semantics), never set
# by hand — there are no branch operations in this slice.
SETTABLE_GOAL_STATUSES = ("waiting", "active", "paused", "complete")
TASK_STATUSES = ("open", "doing", "done")
# Legacy store goal statuses -> durable statuses (migration only).
_LEGACY_STATUS_MAP = {"active": "active", "paused": "paused",
                      "done": "complete"}

_RECORD_COLUMNS = (
    "goal_uid", "human_id", "goal", "status", "revision", "plan",
    "tasks", "backlog", "latest_result", "parent_uid", "fork_revision",
    "provenance", "created_at", "updated_at")
_JSON_COLUMNS = ("plan", "tasks", "backlog", "provenance")


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


def slugify(text: str, fallback: str = "goal") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48].strip("-") or fallback


def _decode(row: sqlite3.Row) -> dict:
    record = {key: row[key] for key in row.keys()}
    for column in _JSON_COLUMNS:
        value = record[column]
        record[column] = json.loads(value) if value is not None else None
    return record


class GoalLedger:
    """The SQLite-backed durable goal store (see module docstring)."""

    def __init__(self, state_dir: Path | str):
        self.state_dir = Path(state_dir).expanduser()
        self.db_path = self.state_dir / "goals.db"
        self.migration_notes: list[dict] = []
        self._init_schema()
        self._migrate_legacy()

    # -- connections / schema ------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS goals (
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
                CREATE TABLE IF NOT EXISTS session_bindings (
                    session_id TEXT PRIMARY KEY,
                    goal_uid TEXT NOT NULL,
                    bound_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS applied_actions (
                    scope TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    applied_at TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    PRIMARY KEY (scope, action_id));
                """)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) "
                "VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
            conn.commit()
        finally:
            conn.close()

    # -- row helpers ------------------------------------------------------

    @staticmethod
    def _fetch(conn: sqlite3.Connection, uid: str) -> dict | None:
        row = conn.execute(
            "SELECT * FROM goals WHERE goal_uid = ?", (uid,)).fetchone()
        return _decode(row) if row is not None else None

    @staticmethod
    def _insert(conn: sqlite3.Connection, record: dict) -> None:
        values = []
        for column in _RECORD_COLUMNS:
            value = record[column]
            if column in _JSON_COLUMNS and value is not None:
                value = json.dumps(value)
            values.append(value)
        conn.execute(
            f"INSERT INTO goals ({', '.join(_RECORD_COLUMNS)}) "
            f"VALUES ({', '.join('?' * len(_RECORD_COLUMNS))})", values)

    @staticmethod
    def _update(conn: sqlite3.Connection, record: dict) -> None:
        assignments = ", ".join(
            f"{c} = ?" for c in _RECORD_COLUMNS if c != "goal_uid")
        values = []
        for column in _RECORD_COLUMNS:
            if column == "goal_uid":
                continue
            value = record[column]
            if column in _JSON_COLUMNS and value is not None:
                value = json.dumps(value)
            values.append(value)
        values.append(record["goal_uid"])
        conn.execute(
            f"UPDATE goals SET {assignments} WHERE goal_uid = ?", values)

    # -- idempotency scopes -------------------------------------------------
    #
    # An action id is checked against every scope the payload could
    # have been recorded under (its goal, its session, the global
    # scope) and recorded under the MOST SPECIFIC one that applies.
    # Ids are model-issued uuids, unique per logical action, so the
    # scopes are storage hygiene, not a security boundary: a replay
    # is caught whichever door it comes back through.

    @staticmethod
    def _check_action(conn: sqlite3.Connection, scopes: list[str],
                      action_id: str | None) -> None:
        if not action_id:
            return
        for scope in scopes:
            row = conn.execute(
                "SELECT outcome FROM applied_actions "
                "WHERE scope = ? AND action_id = ?",
                (scope, action_id)).fetchone()
            if row is not None:
                raise DuplicateAction(action_id, row["outcome"])

    @staticmethod
    def _record_action(conn: sqlite3.Connection, scope: str,
                       action_id: str | None, outcome: str) -> None:
        if not action_id:
            return
        conn.execute(
            "INSERT INTO applied_actions"
            "(scope, action_id, applied_at, outcome) VALUES (?, ?, ?, ?)",
            (scope, action_id, now_iso(), outcome))

    def action_outcome(self, scopes: list[str],
                       action_id: str) -> str | None:
        """The recorded outcome for an action id, or None — the
        read-only pre-check the runner uses before it dispatches."""
        conn = self._connect()
        try:
            for scope in scopes:
                row = conn.execute(
                    "SELECT outcome FROM applied_actions "
                    "WHERE scope = ? AND action_id = ?",
                    (scope, action_id)).fetchone()
                if row is not None:
                    return row["outcome"]
            return None
        finally:
            conn.close()

    # -- reads ---------------------------------------------------------------

    def get(self, uid: str) -> dict | None:
        conn = self._connect()
        try:
            return self._fetch(conn, uid)
        finally:
            conn.close()

    def list_goals(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM goals ORDER BY created_at, goal_uid"
            ).fetchall()
            return [_decode(row) for row in rows]
        finally:
            conn.close()

    def goal_for_session(self, session_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT goal_uid FROM session_bindings "
                "WHERE session_id = ?", (session_id,)).fetchone()
            if row is None:
                return None
            return self._fetch(conn, row["goal_uid"])
        finally:
            conn.close()

    def bound_uid(self, session_id: str) -> str | None:
        record = self.goal_for_session(session_id)
        return record["goal_uid"] if record else None

    # -- bindings -------------------------------------------------------------

    def bind_session(self, session_id: str, uid: str) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if self._fetch(conn, uid) is None:
                raise GoalStoreError(f"no such goal: {uid}")
            conn.execute(
                "INSERT OR REPLACE INTO session_bindings"
                "(session_id, goal_uid, bound_at) VALUES (?, ?, ?)",
                (session_id, uid, now_iso()))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- creation -------------------------------------------------------------

    def record_action(self, scope: str, action_id: str,
                      outcome: str) -> None:
        """Remember an applied action id under a scope (its own
        transaction — for applies, like goal creation, that are not
        record mutations and so don't ride _mutate)."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._record_action(conn, scope, action_id, outcome)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_goal(self, goal_text: str, *, human_id: str = "",
                    status: str = "active",
                    provenance: dict | None = None,
                    session_id: str | None = None,
                    action_id: str | None = None,
                    action_scopes: list[str] | None = None,
                    outcome: str = "") -> dict:
        """Create a durable goal (harness issues the uid, revision 1)
        and optionally bind a session to it — one transaction,
        including the duplicate-action check and recording."""
        if status not in GOAL_STATUSES:
            raise GoalStoreError(
                f"goal status must be one of {GOAL_STATUSES}")
        now = now_iso()
        record = {
            "goal_uid": new_goal_uid(),
            "human_id": human_id or slugify(goal_text)
            or slugify(session_id or "", "goal"),
            "goal": goal_text, "status": status, "revision": 1,
            "plan": None, "tasks": [], "backlog": [],
            "latest_result": "", "parent_uid": None,
            "fork_revision": None, "provenance": provenance or {},
            "created_at": now, "updated_at": now,
        }
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._check_action(conn, action_scopes or [], action_id)
            self._insert(conn, record)
            if session_id is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO session_bindings"
                    "(session_id, goal_uid, bound_at) VALUES (?, ?, ?)",
                    (session_id, record["goal_uid"], now))
            self._record_action(
                conn, record["goal_uid"], action_id,
                outcome or f"goal set: {goal_text}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return record

    def ensure_session_goal(self, session_id: str) -> dict:
        """The session's bound goal, created (empty text) and bound
        when the session has none — the first goal/task mutation of
        a session materialises its durable goal object."""
        record = self.goal_for_session(session_id)
        if record is not None:
            return record
        return self.create_goal(
            "", provenance={"created_for_session": session_id},
            session_id=session_id)

    # -- mutation engine ------------------------------------------------------
    #
    # Every mutation runs through _mutate: ONE transaction that
    # resolves the record, checks the action id (duplicate), checks
    # the expected revision (compare-and-swap), applies the change
    # function, bumps revision + updated_at, records the action id —
    # or rolls the whole thing back.

    def _mutate(self, uid: str, change, *,
                expect_revision: int | None = None,
                action_id: str | None = None,
                action_scopes: list[str] | None = None,
                record_scope: str | None = None,
                outcome: str = ""):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            record = self._fetch(conn, uid)
            if record is None:
                raise GoalStoreError(f"no such goal: {uid}")
            scopes = action_scopes or [uid]
            self._check_action(conn, scopes, action_id)
            if expect_revision is not None \
                    and record["revision"] != expect_revision:
                raise StaleRevision(expect_revision, record["revision"])
            result = change(record)
            record["revision"] += 1
            record["updated_at"] = now_iso()
            self._update(conn, record)
            self._record_action(conn, record_scope or uid, action_id,
                                outcome)
            conn.commit()
            return result, record
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def set_goal_text(self, uid: str, text: str, *,
                      expect_revision: int | None = None,
                      action_id: str | None = None,
                      action_scopes: list[str] | None = None,
                      outcome: str = "") -> dict:
        def change(record):
            record["goal"] = text
            if not record["human_id"]:
                record["human_id"] = slugify(text)
            return record
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

        def change(record):
            record["status"] = status
            return record
        _, record = self._mutate(
            uid, change, expect_revision=expect_revision,
            action_id=action_id, action_scopes=action_scopes,
            outcome=outcome or f"goal status: {status}")
        return record

    def add_tasks(self, uid: str, titles: list[str], *,
                  status: str = "open",
                  expect_revision: int | None = None,
                  action_id: str | None = None,
                  action_scopes: list[str] | None = None,
                  outcome: str = "") -> tuple[list[dict], dict]:
        """Append tasks in one transaction; returns (added, record)."""
        if status not in TASK_STATUSES:
            raise GoalStoreError(
                f"task status must be one of {TASK_STATUSES}")

        def change(record):
            added = [{"title": title, "status": status}
                     for title in titles]
            record["tasks"] = record["tasks"] + added
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
        Returns (task|None, record); task is None when the title is
        unknown — no mutation and no revision bump in that case is
        NOT guaranteed by _mutate (it commits the unchanged record),
        so the lookup happens before _mutate is entered."""
        if status not in TASK_STATUSES:
            raise GoalStoreError(
                f"task status must be one of {TASK_STATUSES}")
        record = self.get(uid)
        if record is None:
            raise GoalStoreError(f"no such goal: {uid}")
        if not any(t["title"] == title for t in record["tasks"]):
            return None, record

        def change(rec):
            for task in rec["tasks"]:
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
        """The atomic batch: validate the SET against the record's
        tasks, then apply — or not at all. Duplicate/conflicting/
        unknown titles return as errors with NOTHING applied (the
        transaction still commits — recording the attempt's action
        id is deliberately NOT done for a rejected batch: a fixed
        re-send carries a new action id, and the batch's own
        no-op semantics already make identical re-sends safe).
        Returns (applied, errors, record-after)."""
        for transition in transitions:
            if transition["status"] not in TASK_STATUSES:
                raise GoalStoreError(
                    f"task status must be one of {TASK_STATUSES}")

        def change(record):
            by_title: dict[str, dict] = {}
            for task in record["tasks"]:
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
                task["status"] = transition["status"]
                applied.append({"title": transition["title"],
                                "from": old, "to": transition["status"],
                                "noop": old == transition["status"]})
            return applied, []
        # A rejected batch must NOT bump the revision: _mutate
        # always bumps, so the batch runs its own transaction here
        # rather than going through _mutate.
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            record = self._fetch(conn, uid)
            if record is None:
                raise GoalStoreError(f"no such goal: {uid}")
            self._check_action(conn, action_scopes or [uid], action_id)
            if expect_revision is not None \
                    and record["revision"] != expect_revision:
                raise StaleRevision(expect_revision, record["revision"])
            applied, errors = change(record)
            if not errors:
                record["revision"] += 1
                record["updated_at"] = now_iso()
                self._update(conn, record)
                self._record_action(
                    conn, uid, action_id,
                    outcome or "task-update applied")
            conn.commit()
            return applied, errors, record
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- export / import -------------------------------------------------------

    def export_document(self, uid: str) -> dict:
        record = self.get(uid)
        if record is None:
            raise GoalStoreError(f"no such goal: {uid}")
        return {"schema": EXPORT_SCHEMA, "exported_at": now_iso(),
                "goal": record}

    def export_all_document(self) -> dict:
        return {"schema": EXPORT_SCHEMA, "exported_at": now_iso(),
                "goals": self.list_goals()}

    def import_document(self, document: dict) -> list[dict]:
        """Import an export document (single `goal` or `goals`
        list). Every record is preserved EXACTLY — uid, revision,
        status, plan, tasks, backlog, provenance, timestamps — and
        an existing goal_uid is refused by name, never overwritten.
        The whole document imports or none of it does."""
        records = _validate_export(document)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for record in records:
                if self._fetch(conn, record["goal_uid"]) is not None:
                    raise GoalStoreError(
                        f"goal {record['goal_uid']} already exists — "
                        f"import refuses to overwrite it")
                self._insert(conn, record)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return records

    # -- legacy migration -------------------------------------------------------

    def _migrate_legacy(self) -> None:
        """One-time import of the legacy per-session JSON store.

        Triggered by the absence of the meta marker. Each legacy
        file with content becomes a durable goal (fresh uid, legacy
        `done` -> `complete`), its session (the filename stem — the
        legacy safe-name transform is exact for ordinary ids) is
        bound, and the file is renamed `<name>.migrated`."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                ("legacy_goals_migrated",)).fetchone()
            if row is not None:
                return
            legacy_dir = self.state_dir / "goals"
            files = (sorted(legacy_dir.glob("*.json"))
                     if legacy_dir.exists() else [])
            conn.execute("BEGIN IMMEDIATE")
            for path in files:
                note = self._migrate_legacy_file(conn, path)
                if note is not None:
                    self.migration_notes.append(note)
                path.rename(path.with_suffix(path.suffix + ".migrated"))
            conn.execute(
                "INSERT INTO meta(key, value) "
                "VALUES ('legacy_goals_migrated', ?)", (now_iso(),))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _migrate_legacy_file(self, conn: sqlite3.Connection,
                             path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        goal = data.get("goal")
        tasks = [t for t in data.get("tasks", [])
                 if isinstance(t, dict) and t.get("title")]
        if goal is None and not tasks:
            return None
        session_id = path.stem
        now = now_iso()
        goal_text = str(goal.get("title", "")) if goal else ""
        status = _LEGACY_STATUS_MAP.get(
            str(goal.get("status", "active")) if goal else "active",
            "active")
        record = {
            "goal_uid": new_goal_uid(),
            "human_id": slugify(goal_text) if goal_text
            else slugify(session_id),
            "goal": goal_text, "status": status, "revision": 1,
            "plan": None,
            "tasks": [{"title": str(t["title"]),
                       "status": t["status"]
                       if t.get("status") in TASK_STATUSES else "open"}
                      for t in tasks],
            "backlog": [], "latest_result": "",
            "parent_uid": None, "fork_revision": None,
            "provenance": {"migrated_from": "legacy session store",
                           "session": session_id,
                           "source_file": path.name},
            "created_at": now, "updated_at": now,
        }
        self._insert(conn, record)
        conn.execute(
            "INSERT OR REPLACE INTO session_bindings"
            "(session_id, goal_uid, bound_at) VALUES (?, ?, ?)",
            (session_id, record["goal_uid"], now))
        return {"session": session_id, "goal_uid": record["goal_uid"],
                "goal": goal_text, "tasks": len(record["tasks"])}


def _validate_export(document) -> list[dict]:
    """Strict shape check of an export document; returns the record
    dicts (JSON fields decoded, timestamps verbatim)."""
    if not isinstance(document, dict):
        raise GoalStoreError("export document must be a JSON object")
    if document.get("schema") != EXPORT_SCHEMA:
        raise GoalStoreError(
            f"export document schema must be {EXPORT_SCHEMA!r}, got "
            f"{document.get('schema')!r}")
    raw_records = []
    if isinstance(document.get("goal"), dict):
        raw_records.append(document["goal"])
    if isinstance(document.get("goals"), list):
        raw_records.extend(document["goals"])
    if not raw_records:
        raise GoalStoreError(
            "export document carries no goal record ('goal' or "
            "'goals')")
    return [_validate_record(raw) for raw in raw_records]


def _validate_record(raw) -> dict:
    if not isinstance(raw, dict):
        raise GoalStoreError("goal record must be a JSON object")
    missing = [c for c in _RECORD_COLUMNS if c not in raw]
    if missing:
        raise GoalStoreError(
            f"goal record is missing fields: {', '.join(missing)}")
    record = {c: raw[c] for c in _RECORD_COLUMNS}
    if not isinstance(record["goal_uid"], str) \
            or not record["goal_uid"]:
        raise GoalStoreError("goal record has no goal_uid")
    if record["status"] not in GOAL_STATUSES:
        raise GoalStoreError(
            f"goal {record['goal_uid']}: status must be one of "
            f"{GOAL_STATUSES}, got {record['status']!r}")
    if not isinstance(record["revision"], int) \
            or record["revision"] < 1:
        raise GoalStoreError(
            f"goal {record['goal_uid']}: revision must be a positive "
            f"integer")
    if not isinstance(record["tasks"], list) or not all(
            isinstance(t, dict) and isinstance(t.get("title"), str)
            and t.get("status") in TASK_STATUSES
            for t in record["tasks"]):
        raise GoalStoreError(
            f"goal {record['goal_uid']}: tasks must be a list of "
            f"{{title, status}} with status in {TASK_STATUSES}")
    if not isinstance(record["backlog"], list) or not all(
            isinstance(b, str) for b in record["backlog"]):
        raise GoalStoreError(
            f"goal {record['goal_uid']}: backlog must be a list of "
            f"strings")
    plan = record["plan"]
    if plan is not None:
        if not isinstance(plan, dict) \
                or not isinstance(plan.get("steps"), list) \
                or not isinstance(plan.get("current_step"), int) \
                or not all(isinstance(s, dict) and "n" in s
                           and "title" in s for s in plan["steps"]):
            raise GoalStoreError(
                f"goal {record['goal_uid']}: plan must be "
                f"{{revision_label, steps: [{{n, title, status}}], "
                f"current_step}}")
    return record


# -- rendering (shared by the payload actions and the CLI) ---------------------


def _current_step(record: dict) -> tuple[int, str | None] | None:
    """(n, title|None) for the plan's current step, or None when the
    goal carries no plan."""
    plan = record.get("plan")
    if not plan:
        return None
    current = plan.get("current_step")
    title = next((s["title"] for s in plan.get("steps", [])
                  if s.get("n") == current), None)
    return current, title


def render_section(record: dict | None) -> str:
    """The sticky footer section projected from a durable record.

    Fixed shape, fixed position (always the last section of a bound
    result block): the [goal] line (goal text, status, short uid),
    a [step] line ONLY when the goal carries a plan, and the
    [open tasks] line. A goal-less session renders the legacy
    "(none set)" shape."""
    if record is None or not record["goal"]:
        lines = ["[goal] (none set)"]
        tasks = record["tasks"] if record else []
    else:
        lines = [f"[goal] {record['goal']} [{record['status']}] "
                 f"({record['goal_uid'][:8]})"]
        step = _current_step(record)
        if step:
            number, title = step
            lines.append(f"[step] {number}"
                         + (f" — {title}" if title else ""))
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
        line = (f"  {record['goal_uid']} · {record['human_id']} · "
                f"{record['status']} · rev {record['revision']}")
        step = _current_step(record)
        if step:
            number, title = step
            line += f" · step {number}" + (f": {title}" if title
                                           else "")
        lines.append(line)
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
    plan = record.get("plan")
    if plan:
        lines.append(f"plan: {plan.get('revision_label', '')} · "
                     f"current step {plan.get('current_step')}")
        for step in plan.get("steps", []):
            lines.append(f"  {step['n']}. "
                         f"[{step.get('status', 'open')}] "
                         f"{step['title']}")
    if record["tasks"]:
        lines.append("tasks:")
        for task in record["tasks"]:
            lines.append(f"  [{task['status']}] {task['title']}")
    else:
        lines.append("tasks: none")
    if record["backlog"]:
        lines.append("backlog:")
        for item in record["backlog"]:
            lines.append(f"  - {item}")
    if record["latest_result"]:
        lines.append(f"latest result: {record['latest_result']}")
    if record["parent_uid"]:
        lines.append(f"parent: {record['parent_uid']} · "
                     f"forked at revision {record['fork_revision']}")
    if record["provenance"]:
        lines.append("provenance: " + json.dumps(
            record["provenance"], ensure_ascii=False, sort_keys=True))
    lines.append(f"created: {record['created_at']} · "
                 f"updated: {record['updated_at']}")
    return "\n".join(lines)
