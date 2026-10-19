"""The run ledger: one JSONL line per lifecycle event per run.

This is the harness's evidence trail (the transcript_path analogue from
the hooks study). Two rules are load-bearing:

  1. A run's receipt entries are written SYNCHRONOUSLY, and the PostRun /
     PostRunFailure entry is written BEFORE the clipboard is overwritten
     or the notification fires — the receipt never lags reality.
  2. Entries are append-only JSONL, one object per line, each carrying
     run_id, event, timestamp, and the event payload.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from .core import now_iso


class Ledger:
    def __init__(self, path: Path | str):
        self.path = Path(path).expanduser()
        self._lock = threading.Lock()

    # -- writing ---------------------------------------------------------

    def append(self, run_id: str, event: str, data: dict[str, Any] | None = None,
               session_id: str | None = None,
               binding_id: str | None = None) -> dict[str, Any]:
        entry = {
            "ts": now_iso(),
            "run_id": run_id,
            "event": event,
            "session_id": session_id,
            "binding_id": binding_id,
            "data": data or {},
        }
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
        return entry

    # -- reading ---------------------------------------------------------

    def entries(self, run_id: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if run_id is None or entry.get("run_id") == run_id:
                    out.append(entry)
        return out

    def run_ids(self) -> list[str]:
        seen: list[str] = []
        for entry in self.entries():
            rid = entry.get("run_id")
            if rid and rid not in seen:
                seen.append(rid)
        return seen

    def summary(self) -> dict[str, int]:
        """Counts by terminal event — the SessionEnd rollup."""
        counts: dict[str, int] = {}
        for entry in self.entries():
            event = entry.get("event", "")
            counts[event] = counts.get(event, 0) + 1
        return counts


class Transcript:
    """The human-readable markdown transcript (the original ~/agent-loop.md,
    kept because Ali reads it). One section per finished run."""

    def __init__(self, path: Path | str):
        self.path = Path(path).expanduser()

    def append_run(self, when: str, command: str, output: str,
                   exit_desc: str, elapsed: float) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(
                f"\n## {when}\n$ {command}\n{output}\n"
                f"[exit {exit_desc} · {elapsed:.1f}s]\n"
            )
