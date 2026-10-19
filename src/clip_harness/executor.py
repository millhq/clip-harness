"""Command execution — the local backend.

The original loop ran commands inside an iTerm2 tab via a sentinel
file; this executor runs them directly with the user's shell, streaming
output to a file on disk (the spill file the result block points at),
with cooperative cancel and a hard timeout. The iTerm2 backend lives in
adapters/iterm2_watch.py and implements the same interface — the
lifecycle runner depends on the interface, never on a backend.

Live visibility is an optional layer on top of the same capture: when
a ``sink`` callable is supplied, output is pumped through a pipe so
each chunk ALSO goes to the sink (the terminal) as it arrives, while
the file keeps accumulating exactly the bytes the plain path would
capture. Silence past ``HEARTBEAT_INTERVAL_S`` emits a short elapsed
note to the sink only. The sink is presentation — like diff color, it
never touches the captured output, the spill file, or the result.

Sandboxing is the same shape of layer: when a ``sandbox_profile`` is
supplied (a reviewed command accepted SANDBOXED), the spawn argv is
prefixed with `sandbox-exec -p <profile>` — supervision, timeout and
cancel are unchanged, and the streamed path composes identically.
If sandbox-exec is not available the run FAILS CLOSED: nothing
executes and the result says why. A sandboxed command never runs
bare by accident.
"""

from __future__ import annotations

import codecs
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Protocol

from .core import ExecResult

#: Seconds of output silence before the streaming path tells the
#: terminal the run is still alive (and between repeats). Tests
#: inject a shorter interval per run; None selects this default.
HEARTBEAT_INTERVAL_S = 10.0


class Executor(Protocol):
    def run(self, command: str, cwd: str | Path, timeout: float,
            cancel_event: threading.Event, output_path: Path,
            sink: Callable[[str], None] | None = None,
            heartbeat_interval: float | None = None,
            sandbox_profile: str | None = None) -> ExecResult:
        ...


class _StreamState:
    """State shared between the pump thread and the supervising loop
    of a streamed run: when output last arrived, whether the stream
    currently ends at a line boundary (so a heartbeat lands on its
    own line), and whether the sink has failed (a dead terminal must
    never wedge the run — the sink is muted, capture continues)."""

    def __init__(self) -> None:
        self.last_output_at = time.monotonic()
        self.at_line_start = True
        self.sink_failed = False


def _emit(sink: Callable[[str], None], state: _StreamState,
          text: str) -> None:
    if state.sink_failed or not text:
        return
    try:
        sink(text)
    except Exception:
        state.sink_failed = True


class LocalExecutor:
    """Runs the payload command in a subprocess, output streamed to a
    file so progress is visible live (tail -f) and the full output
    survives the 128k clipboard cap."""

    def __init__(self, shell: str = "/bin/sh"):
        self.shell = shell

    def _spawn_argv(self, command: str,
                    sandbox_profile: str | None) -> list[str]:
        """The process argv for one run: the shell invocation, with
        the Seatbelt wrapper as a prefix when the run is sandboxed."""
        argv = [self.shell, "-c", command]
        if sandbox_profile is not None:
            argv = ["sandbox-exec", "-p", sandbox_profile, *argv]
        return argv

    def run(self, command: str, cwd: str | Path, timeout: float,
            cancel_event: threading.Event, output_path: Path,
            sink: Callable[[str], None] | None = None,
            heartbeat_interval: float | None = None,
            sandbox_profile: str | None = None) -> ExecResult:
        cwd = Path(cwd)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if sandbox_profile is not None \
                and shutil.which("sandbox-exec") is None:
            # Fail closed: a sandboxed accept must never degrade
            # into a bare run. Nothing executes; the result block
            # carries the reason (and the human's recourse is an
            # explicit unsandboxed accept at a fresh review).
            message = ("sandboxed execution unavailable: sandbox-exec "
                       "not found on this machine — the command was "
                       "NOT run. Re-review and accept it unsandboxed "
                       "explicitly if it should run bare.")
            output_path.write_text(message + "\n", encoding="utf-8")
            return ExecResult(output=message + "\n",
                              exit_desc="error: sandbox-exec unavailable",
                              exit_code=None, elapsed=0.0,
                              output_file=str(output_path))
        if sink is not None:
            return self._run_streamed(command, cwd, timeout,
                                      cancel_event, output_path, sink,
                                      heartbeat_interval, sandbox_profile)
        started = time.monotonic()
        cancelled = timed_out = False
        with open(output_path, "wb") as out:
            proc = subprocess.Popen(
                self._spawn_argv(command, sandbox_profile),
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
        return self._finish(output_path, started, proc, cancelled,
                            timed_out)

    def _run_streamed(self, command: str, cwd: Path, timeout: float,
                      cancel_event: threading.Event, output_path: Path,
                      sink: Callable[[str], None],
                      heartbeat_interval: float | None,
                      sandbox_profile: str | None = None) -> ExecResult:
        """The capture path with a live terminal view: the child's
        merged stdout/stderr flows through a pipe; a pump thread
        writes each chunk to the spill file (flushed, so tail -f
        follows live) and forwards the decoded text to the sink.
        The supervising loop is the plain path's — same cancel flag,
        same timeout bound, same stop semantics — plus the silence
        heartbeat, which goes to the sink only."""
        interval = (HEARTBEAT_INTERVAL_S if heartbeat_interval is None
                    else heartbeat_interval)
        started = time.monotonic()
        cancelled = timed_out = False
        state = _StreamState()
        with open(output_path, "wb") as out:
            proc = subprocess.Popen(
                self._spawn_argv(command, sandbox_profile),
                cwd=str(cwd), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True)
            pump = threading.Thread(
                target=self._pump, args=(proc, out, sink, state),
                daemon=True)
            pump.start()
            last_heartbeat = started
            while proc.poll() is None:
                if cancel_event.is_set():
                    cancelled = True
                    self._stop(proc)
                    break
                now = time.monotonic()
                if now - started > timeout:
                    timed_out = True
                    self._stop(proc)
                    break
                if interval > 0 \
                        and now - state.last_output_at >= interval \
                        and now - last_heartbeat >= interval:
                    prefix = "" if state.at_line_start else "\n"
                    _emit(sink, state,
                          f"{prefix}… running {int(now - started)}s\n")
                    state.at_line_start = True
                    last_heartbeat = now
                time.sleep(0.05)
            proc.wait()
            # Drain whatever the pump still holds so the spill file is
            # complete before it is read back. A grandchild that
            # inherited the pipe can hold it open past the child's
            # exit; bound the wait, then close our end — capture of
            # the command's own output is already complete.
            pump.join(timeout=5)
            if pump.is_alive():
                try:
                    proc.stdout.close()
                except OSError:
                    pass
                pump.join(timeout=1)
        return self._finish(output_path, started, proc, cancelled,
                            timed_out)

    @staticmethod
    def _pump(proc: subprocess.Popen, out, sink: Callable[[str], None],
              state: _StreamState) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(
            errors="replace")
        fd = proc.stdout.fileno()
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            out.write(chunk)
            out.flush()
            state.last_output_at = time.monotonic()
            state.at_line_start = chunk.endswith(b"\n")
            text = decoder.decode(chunk)
            if text:
                _emit(sink, state, text)
        tail = decoder.decode(b"", final=True)
        if tail:
            _emit(sink, state, tail)

    @staticmethod
    def _finish(output_path: Path, started: float,
                proc: subprocess.Popen, cancelled: bool,
                timed_out: bool) -> ExecResult:
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
