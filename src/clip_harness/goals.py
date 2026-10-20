"""Goal/task state — CORE, deliberately not a hook.

Ali's rule: behaviour that must ALWAYS happen lives in the core; hooks
are only for optional extension. The goal/task nag is the canonical
example of the first kind — it exists so the chat model is reminded of
the active goal and open tasks on every action and doesn't drift. A nag
that depends on an optional, removable, fail-open hook is not a nag.

The state itself is DURABLE (see goalstore.py): goals are first-class
objects in a SQLite ledger, issued an immutable goal_uid, referenced
by sessions through a binding — never owned by them. This module is
the session-bound VIEW over that ledger: given a session id it
resolves the bound goal and offers the goal/task operations the
runner and the terminal CLI drive. Unbound runs anchor to nothing:
their result blocks carry no goal section.
"""

from __future__ import annotations

from pathlib import Path

from .goalstore import (GOAL_STATUSES, SETTABLE_GOAL_STATUSES,
                        TASK_STATUSES, DuplicateAction, GoalLedger,
                        GoalStoreError, StaleRevision, render_section)

__all__ = ["GOAL_STATUSES", "SETTABLE_GOAL_STATUSES", "TASK_STATUSES",
           "DuplicateAction", "GoalLedger", "GoalStore",
           "GoalStoreError", "StaleRevision", "render_section"]


class GoalStore:
    """The goal/task view of ONE session binding over the ledger."""

    def __init__(self, state_dir: Path | str, binding_id: str):
        self.binding_id = binding_id
        self.ledger = GoalLedger(state_dir)

    # -- binding / record ---------------------------------------------------

    def bound_uid(self) -> str | None:
        return self.ledger.bound_uid(self.binding_id)

    def record(self) -> dict | None:
        """The bound durable record, or None when the session has
        never materialised one. Reads never create state."""
        return self.ledger.goal_for_session(self.binding_id)

    def _ensure_record(self) -> dict:
        return self.ledger.ensure_session_goal(self.binding_id)

    def _scopes(self, action_scopes: list[str] | None) -> list[str]:
        """Idempotency check scopes for a mutation of the bound
        goal: the goal itself (once known), this session, global."""
        scopes = list(action_scopes or [])
        uid = self.bound_uid()
        if uid and uid not in scopes:
            scopes.insert(0, uid)
        session_scope = f"session:{self.binding_id}"
        if session_scope not in scopes:
            scopes.append(session_scope)
        if "" not in scopes:
            scopes.append("")
        return scopes

    # -- goal ---------------------------------------------------------------

    def goal(self) -> dict | None:
        """The bound goal as the session view's dict:
        {title, status, uid, human_id, revision} — or None."""
        record = self.record()
        if record is None or not record["goal"]:
            return None
        return {"title": record["goal"], "status": record["status"],
                "uid": record["goal_uid"],
                "human_id": record["human_id"],
                "revision": record["revision"]}

    def set_goal(self, title: str, status: str = "active", *,
                 expect_revision: int | None = None,
                 action_id: str | None = None,
                 action_scopes: list[str] | None = None,
                 outcome: str = "") -> dict:
        """Set the bound goal's text — creating the durable goal
        (bound to this session, harness-issued uid) on first use.
        Returns the goal view dict."""
        if status not in GOAL_STATUSES:
            raise GoalStoreError(
                f"goal status must be one of {GOAL_STATUSES}")
        record = self.record()
        if record is None:
            record = self.ledger.create_goal(
                title, status=status, session_id=self.binding_id,
                action_id=action_id,
                action_scopes=self._scopes(action_scopes),
                outcome=outcome or f"goal set: {title}")
            created = True
        else:
            # The legacy semantics ride along: goal-set carries a
            # status (default active) as well as the text. The guards
            # apply once, to the text mutation; a differing status is
            # a consequence of the same action and follows unguarded.
            record = self.ledger.set_goal_text(
                record["goal_uid"], title,
                expect_revision=expect_revision, action_id=action_id,
                action_scopes=self._scopes(action_scopes),
                outcome=outcome or f"goal set: {title}")
            if status != record["status"]:
                record = self.ledger.set_status(
                    record["goal_uid"], status)
            created = False
        return {"title": record["goal"], "status": record["status"],
                "uid": record["goal_uid"],
                "human_id": record["human_id"],
                "revision": record["revision"], "created": created}

    def set_goal_status(self, status: str, *,
                        expect_revision: int | None = None,
                        action_id: str | None = None,
                        action_scopes: list[str] | None = None,
                        outcome: str = "") -> dict | None:
        record = self.record()
        if record is None or not record["goal"]:
            return None
        record = self.ledger.set_status(
            record["goal_uid"], status,
            expect_revision=expect_revision, action_id=action_id,
            action_scopes=self._scopes(action_scopes),
            outcome=outcome or f"goal status: {status}")
        return {"title": record["goal"], "status": record["status"],
                "uid": record["goal_uid"],
                "human_id": record["human_id"],
                "revision": record["revision"]}

    # -- tasks ----------------------------------------------------------------

    def add_task(self, title: str, status: str = "open") -> dict:
        added, _ = self.add_tasks([title], status=status)
        return added[0]

    def add_tasks(self, titles: list[str], status: str = "open", *,
                  expect_revision: int | None = None,
                  action_id: str | None = None,
                  action_scopes: list[str] | None = None,
                  outcome: str = "") -> tuple[list[dict], dict]:
        record = self._ensure_record()
        return self.ledger.add_tasks(
            record["goal_uid"], list(titles), status=status,
            expect_revision=expect_revision, action_id=action_id,
            action_scopes=self._scopes(action_scopes),
            outcome=outcome)

    def set_task_status(self, title: str, status: str) -> dict | None:
        if status not in TASK_STATUSES:
            raise GoalStoreError(
                f"task status must be one of {TASK_STATUSES}")
        record = self._ensure_record()
        task, _ = self.ledger.set_task_status(
            record["goal_uid"], title, status)
        return task

    def apply_task_transitions(
            self, transitions: list[dict], *,
            expect_revision: int | None = None,
            action_id: str | None = None,
            action_scopes: list[str] | None = None,
            outcome: str = "") -> tuple[list[dict], list[dict]]:
        """Validate a batch of status transitions, then apply — or not.

        Anthropic's converged shape: judge the SET, not each op. Every
        title is resolved against the current task list first; if any
        title is unknown, duplicated (same title, same status twice)
        or conflicting (same title, different statuses), NOTHING is
        applied and the errors list names each offending title with
        its problem. Re-setting a task to the status it already has
        is a valid no-op transition, reported as such, so idempotent
        re-sends never fail. The guards ride the same transaction:
        a duplicate action id raises DuplicateAction and a stale
        expected revision raises StaleRevision before any of that.

        Returns (applied, errors): applied entries are
        {"title", "from", "to", "noop"} in batch order.
        """
        record = self._ensure_record()
        applied, errors, _ = self.ledger.apply_task_transitions(
            record["goal_uid"], transitions,
            expect_revision=expect_revision, action_id=action_id,
            action_scopes=self._scopes(action_scopes),
            outcome=outcome)
        return applied, errors

    def tasks(self) -> list[dict]:
        record = self.record()
        return list(record["tasks"]) if record else []

    def open_tasks(self) -> list[dict]:
        return [t for t in self.tasks() if t["status"] != "done"]

    # -- the standing section ---------------------------------------------------

    def section(self) -> str:
        """The core-generated nag appended to every bound result block:
        the sticky footer projected from the bound durable record.
        Present every time, generated here — never by a hook."""
        return render_section(self.record())
