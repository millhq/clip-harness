"""Session registry: the binding between payload session ids and runs.

Spec (Ali, 2026-10-18): the payload always carries commands. A payload MAY
carry a unique id — when it does, the run is BOUND to that session:
addressable by id for cancel/rerun, its receipt keyed by the id. When it
does not, the run is UNBOUND: it still runs (a missing id never blocks
execution), it is simply not addressable afterwards beyond "the last run".

Bound sessions are persisted as one JSON file per id under
<state_dir>/sessions/ so a separate CLI process (`clip-harness sessions`,
`clip-harness cancel <id>`) can see and signal them; in-process runs also
carry a threading.Event for immediate cancellation.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RunHandle:
    run_id: str
    command: str
    cwd: str
    binding_id: str | None = None  # payload session id, None when unbound
    state: str = "captured"
    output_file: str = ""
    started_at: float = field(default_factory=time.time)
    _cancel_event: threading.Event = field(default_factory=threading.Event,
                                            repr=False)

    # -- cancellation ------------------------------------------------------

    def request_cancel(self) -> None:
        self._cancel_event.set()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    def cancel_event(self) -> threading.Event:
        return self._cancel_event

    # -- serialisation -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "binding_id": self.binding_id,
            "command": self.command,
            "cwd": self.cwd,
            "state": self.state,
            "output_file": self.output_file,
            "started_at": self.started_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunHandle":
        return cls(
            run_id=str(data["run_id"]),
            command=str(data.get("command", "")),
            cwd=str(data.get("cwd", "")),
            binding_id=data.get("binding_id"),
            state=str(data.get("state", "captured")),
            output_file=str(data.get("output_file", "")),
            started_at=float(data.get("started_at", time.time())),
        )


class SessionRegistry:
    def __init__(self, state_dir: Path | str):
        self.dir = Path(state_dir).expanduser() / "sessions"
        self._bound: dict[str, RunHandle] = {}
        self._last: RunHandle | None = None
        self._lock = threading.Lock()

    # -- registration --------------------------------------------------------

    def register(self, handle: RunHandle) -> None:
        """Record a run. Bound runs become addressable by their id;
        unbound runs are only remembered as the most recent run."""
        with self._lock:
            self._last = handle
            if handle.binding_id is not None:
                self._bound[handle.binding_id] = handle
                self._persist(handle)

    def bind(self, binding_id: str, handle: RunHandle) -> None:
        handle.binding_id = binding_id
        self.register(handle)

    # -- lookup ----------------------------------------------------------------

    def get(self, binding_id: str) -> RunHandle | None:
        with self._lock:
            handle = self._bound.get(binding_id)
        if handle is not None:
            return handle
        return self._load(binding_id)

    def last(self) -> RunHandle | None:
        if self._last is not None:
            return self._last
        # Fall back to the newest persisted bound session.
        handles = self.list_bound()
        return handles[-1] if handles else None

    def is_bound(self, binding_id: str) -> bool:
        return self.get(binding_id) is not None

    def list_bound(self) -> list[RunHandle]:
        with self._lock:
            handles = dict(self._bound)
        if self.dir.exists():
            for path in sorted(self.dir.glob("*.json")):
                try:
                    handle = RunHandle.from_dict(
                        json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError, KeyError):
                    continue
                if handle.binding_id:
                    handles.setdefault(handle.binding_id, handle)
        return sorted(handles.values(), key=lambda h: h.started_at)

    # -- state / cancel signalling ----------------------------------------------

    def set_state(self, handle: RunHandle, state: str) -> None:
        handle.state = state
        if handle.binding_id is not None:
            with self._lock:
                self._bound[handle.binding_id] = handle
                self._persist(handle)

    def request_cancel(self, binding_id: str | None = None) -> bool:
        """Cancel the run bound to `binding_id` (or the last run when None).

        Returns True when a live handle was signalled or a cancel flag file
        was written for a run owned by another process.
        """
        handle = self.get(binding_id) if binding_id else self.last()
        if handle is None:
            return False
        handle.request_cancel()
        if handle.binding_id is not None:
            flag = self.dir / f"{_safe(handle.binding_id)}.cancel"
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                flag.write_text("cancel requested\n", encoding="utf-8")
            except OSError:
                pass
        return True

    def cancel_flag_set(self, binding_id: str) -> bool:
        return (self.dir / f"{_safe(binding_id)}.cancel").exists()

    def clear_cancel_flag(self, binding_id: str) -> None:
        try:
            (self.dir / f"{_safe(binding_id)}.cancel").unlink()
        except OSError:
            pass

    # -- persistence -------------------------------------------------------------

    def _path_for(self, binding_id: str) -> Path:
        return self.dir / f"{_safe(binding_id)}.json"

    def _persist(self, handle: RunHandle) -> None:
        if handle.binding_id is None:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._path_for(handle.binding_id).write_text(
                json.dumps(handle.to_dict(), indent=2), encoding="utf-8")
        except OSError:
            pass

    def _load(self, binding_id: str) -> RunHandle | None:
        path = self._path_for(binding_id)
        try:
            handle = RunHandle.from_dict(
                json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, KeyError):
            return None
        with self._lock:
            self._bound[binding_id] = handle
        return handle


def _safe(binding_id: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in binding_id)
