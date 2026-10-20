"""Goal/task state — CORE, deliberately not a hook.

Ali's rule: behaviour that must ALWAYS happen lives in the core; hooks
are only for optional extension. The goal/task nag is the canonical
example of the first kind — it exists so the chat model is reminded of
the active goal and open tasks on every action and doesn't drift. A nag
that depends on an optional, removable, fail-open hook is not a nag.

Semantics carried over from his mise suite (goal-create/list/show/
status/update): one active goal per session, plus a task list with
statuses. State is stored per session binding under
<state_dir>/goals/<session-id>.json. Unbound runs have no session and
therefore anchor to nothing: their result blocks carry no goal section.
"""

from __future__ import annotations

import json
from pathlib import Path

GOAL_STATUSES = ("active", "paused", "done")
TASK_STATUSES = ("open", "doing", "done")


class GoalStore:
    def __init__(self, state_dir: Path | str, binding_id: str):
        self.binding_id = binding_id
        safe = "".join(
            c if c.isalnum() or c in "._-" else "_" for c in binding_id)
        self.path = Path(state_dir).expanduser() / "goals" / f"{safe}.json"
        self._data = self._load()

    # -- persistence ------------------------------------------------------

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("goal", None)
                data.setdefault("tasks", [])
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {"goal": None, "tasks": []}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, indent=2), encoding="utf-8")

    # -- goal ---------------------------------------------------------------

    def set_goal(self, title: str, status: str = "active") -> dict:
        if status not in GOAL_STATUSES:
            raise ValueError(f"goal status must be one of {GOAL_STATUSES}")
        self._data["goal"] = {"title": title, "status": status}
        self._save()
        return self._data["goal"]

    def set_goal_status(self, status: str) -> dict | None:
        if self._data["goal"] is None:
            return None
        return self.set_goal(self._data["goal"]["title"], status)

    def goal(self) -> dict | None:
        return self._data["goal"]

    # -- tasks ----------------------------------------------------------------

    def add_task(self, title: str, status: str = "open") -> dict:
        if status not in TASK_STATUSES:
            raise ValueError(f"task status must be one of {TASK_STATUSES}")
        task = {"title": title, "status": status}
        self._data["tasks"].append(task)
        self._save()
        return task

    def set_task_status(self, title: str, status: str) -> dict | None:
        if status not in TASK_STATUSES:
            raise ValueError(f"task status must be one of {TASK_STATUSES}")
        for task in self._data["tasks"]:
            if task["title"] == title:
                task["status"] = status
                self._save()
                return task
        return None

    def apply_task_transitions(
            self, transitions: list[dict]) -> tuple[list[dict], list[dict]]:
        """Validate a batch of status transitions, then apply — or not.

        Anthropic's converged shape: judge the SET, not each op. Every
        title is resolved against the current task list first; if any
        title is unknown, duplicated (same title, same status twice)
        or conflicting (same title, different statuses), NOTHING is
        applied — the store is not even written — and the errors list
        names each offending title with its problem. Re-setting a
        task to the status it already has is a valid no-op
        transition, reported as such, so idempotent re-sends never
        fail.

        Returns (applied, errors): applied entries are
        {"title", "from", "to", "noop"} in batch order.
        """
        for transition in transitions:
            if transition["status"] not in TASK_STATUSES:
                raise ValueError(
                    f"task status must be one of {TASK_STATUSES}")
        by_title: dict[str, dict] = {}
        for task in self._data["tasks"]:
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
            applied.append({"title": transition["title"], "from": old,
                            "to": transition["status"],
                            "noop": old == transition["status"]})
        self._save()
        return applied, []

    def tasks(self) -> list[dict]:
        return list(self._data["tasks"])

    def open_tasks(self) -> list[dict]:
        return [t for t in self._data["tasks"] if t["status"] != "done"]

    # -- the standing section ---------------------------------------------------

    def section(self) -> str:
        """The core-generated nag appended to every bound result block.

        Present every time, generated here — never by a hook.
        """
        goal = self._data["goal"]
        if goal is None:
            goal_line = "[goal] (none set)"
        else:
            goal_line = f"[goal] {goal['title']} [{goal['status']}]"
        open_tasks = self.open_tasks()
        if open_tasks:
            listing = " · ".join(
                f"[{t['status']}] {t['title']}" for t in open_tasks)
            tasks_line = f"[open tasks] {listing}"
        else:
            tasks_line = "[open tasks] none"
        return f"{goal_line}\n{tasks_line}"
