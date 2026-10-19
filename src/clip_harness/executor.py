"""Command execution — the local backend.

The original loop ran commands inside an iTerm2 tab via a sentinel
file; this executor runs them directly with the user's shell, streaming
output to a file on disk (the spill file the result block points at),
with cooperative cancel and a hard timeout. The iTerm2 backend lives in
adapters/iterm2_watch.py and implements the same interface — the
lifecycle runner depends on the interface, never on a backend.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Protocol

from .core import ExecResult


class Executor(Protocol):
    def run(self, command: str, cwd: str | Path, timeout: float,
            cancel_event: threading.Event, output_path: Path) -> ExecResult:
        ...


class LocalExecutor:
    """Runs the payload command in a subprocess, output streamed to a
    file so progress is visible live (tail -f) and the full output
    survives the 128k clipboard cap."""

    def __init__(self, shell: str = "/bin/sh"):
        self.shell = shell

    def run(self, command: str, cwd: str | Path, timeout: float,
            cancel_event: threading.Event, output_path: Path) -> ExecResult:
        cwd = Path(cwd)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        cancelled = timed_out = False
        with open(output_path, "wb") as out:
            proc = subprocess.Popen(
                [self.shell, "-c", command],
                cwd=str(cwd), stdout=out, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True)
            while proc.poll() is None:
                if cancel_event.is_set():
                    cancelled = True
                    self._stop(proc)
                    break
                if time.monotonic() - started > timeout:
                    timed_out = True
                    self._stop(proc)
                    break
                time.sleep(0.05)
            proc.wait()
        duration = time.monotonic() - started
        output = output_path.read_text(encoding="utf-8", errors="replace")
        exit_code = None if (cancelled or timed_out) else proc.returncode
        if cancelled:
            desc = "cancelled"
        elif timed_out:
            desc = "timeout"
        else:
            desc = str(proc.returncode)
        return ExecResult(output=output, exit_desc=desc,
                          exit_code=exit_code, elapsed=duration,
                          timed_out=timed_out, cancelled=cancelled,
                          output_file=str(output_path))

    @staticmethod
    def _stop(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGINT)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
