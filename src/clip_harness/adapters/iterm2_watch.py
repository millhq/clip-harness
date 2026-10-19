"""iTerm2 watch backend — UNTESTED ON MAC (written on Linux).

Adapted from Ali's original clipboard_loop.py AutoLaunch script,
which ran live on his Mac:

- runs inside iTerm2's Python runtime (AutoLaunch), polling the
  NSPasteboard changeCount via AppKit;
- a dedicated "agent-loop" tab hosts the runs — a bound payload's
  session id titles its tab, so the tab IS the session surface;
- commands execute IN the tab through the original sentinel protocol
  (the command is wrapped to redirect output to a file and drop a
  sentinel with the exit code), so Ali can watch and type live;
- cancel sends Ctrl-C to the tab.

Differences from the original: parsing, gating, receipts, sessions and
the review flow are the core's job now — this module only maps the
core onto iTerm2. In watch mode the review decider is the
decision.json file-drop: file payloads stage a candidate and park;
Ali reviews in a terminal and completes the run with
`clip-harness candidate review <id>` — the terminal is the default
review surface, with VS Code available as an option inside that
prompt (it never opens on its own).

Requires: iTerm2 with the Python API enabled, the `iterm2` pip module
(the proven work-Mac deploy path), run as an AutoLaunch script.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from ..core import ExecResult
from ..render import printer
from ..runner import LifecycleRunner
from .headless import FileDropDecider
from .macos import PbClipboard

TAB_TITLE = "agent-loop"
CLAIM_MARKER = "⏳ running in iTerm2 — "


class ITerm2TabExecutor:
    """Executor interface over the iTerm2 tab + sentinel protocol."""

    def __init__(self, session, loop: asyncio.AbstractEventLoop,
                 output_file: str = "/tmp/agent-loop.out"):
        self._session = session
        self._loop = loop
        self.output_file = output_file
        self.sentinel = f"/tmp/agent-loop-{os.getpid()}.done"

    def run(self, command: str, cwd, timeout: float,
            cancel_event: threading.Event,
            output_path: Path, sink=None,
            heartbeat_interval: float | None = None,
            sandbox_profile: str | None = None) -> ExecResult:
        # sink / heartbeat_interval are accepted for interface parity
        # with LocalExecutor and ignored: the command runs visibly in
        # its own iTerm2 tab (that IS the live view), and the watch
        # wiring never enables streaming anyway.
        if sandbox_profile is not None:
            # Fail closed: a command accepted SANDBOXED must never
            # run bare in the tab — this backend cannot wrap the tab's
            # shell in sandbox-exec. Nothing executes.
            message = ("sandboxed execution is not supported by the "
                       "iTerm2 backend — the command was NOT run. "
                       "Review it from a terminal (candidate review) "
                       "or accept it unsandboxed explicitly.")
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_text(message + "\n", encoding="utf-8")
            return ExecResult(
                output=message + "\n",
                exit_desc="error: sandbox unavailable in this backend",
                exit_code=None, elapsed=0.0,
                output_file=str(output_path))
        future = asyncio.run_coroutine_threadsafe(
            self._run_in_tab(command, str(cwd), timeout, cancel_event,
                             Path(output_path)),
            self._loop)
        return future.result(timeout=timeout + 60)

    async def _run_in_tab(self, command: str, cwd: str, timeout: float,
                          cancel_event: threading.Event,
                          output_path: Path) -> ExecResult:
        out = str(output_path)
        wrapped = (
            f"cd {self._q(cwd)} && rm -f {self.sentinel} {self._q(out)}; "
            f"(\n{command}\n) > {self._q(out)} 2>&1 & pid=$!; "
            f"trap 'kill $pid 2>/dev/null; echo 130 > {self.sentinel}' INT; "
            f"wait $pid; echo $? > {self.sentinel}")
        started = time.monotonic()
        await self._session.async_send_text(wrapped + "\n")
        cancelled = timed_out = False
        while not os.path.exists(self.sentinel):
            if cancel_event.is_set() and not cancelled:
                cancelled = True
                await self._session.async_send_text("\x03")
            if time.monotonic() - started > timeout and not timed_out:
                timed_out = True
                await self._session.async_send_text("\x03")
            await asyncio.sleep(0.2)
        try:
            code = int(Path(self.sentinel).read_text().strip() or "1")
        except (OSError, ValueError):
            code = 1
        output = ""
        if os.path.exists(out):
            output = Path(out).read_text(encoding="utf-8", errors="replace")
        if cancelled:
            desc = "cancelled"
        elif timed_out:
            desc = "timeout"
        else:
            desc = str(code)
        return ExecResult(output=output, exit_desc=desc,
                          exit_code=None if (cancelled or timed_out)
                          else code,
                          elapsed=time.monotonic() - started,
                          timed_out=timed_out, cancelled=cancelled,
                          output_file=out)

    @staticmethod
    def _q(text: str) -> str:
        return "'" + text.replace("'", "'\\''") + "'"


async def _get_tab(connection, app):
    """Find or create the dedicated agent-loop tab (original logic)."""
    window = app.current_terminal_window
    if window is None:
        return None, None
    for tab in window.tabs:
        for session in tab.sessions:
            name = await session.async_get_variable("user.tab_title")
            if name == TAB_TITLE or TAB_TITLE in (session.name or ""):
                return tab, session
    tab = await window.async_create_tab()
    session = tab.current_session
    await session.async_set_variable("user.tab_title", TAB_TITLE)
    await session.async_send_text("cd ~ && clear\n")
    return tab, session


async def main(connection, settings, state_dir: Path, cwd: str) -> None:
    import iterm2
    from AppKit import NSPasteboard

    app = await iterm2.async_get_app(connection)
    _tab, session = await _get_tab(connection, app)
    if session is None:
        print("clip-harness: no iTerm2 window available")
        return
    loop = asyncio.get_running_loop()
    runner = LifecycleRunner(
        settings=settings, clipboard=PbClipboard(),
        decider=FileDropDecider(timeout_s=0),
        executor=ITerm2TabExecutor(session, loop),
        presenter=printer(settings.color))
    pasteboard = NSPasteboard.generalPasteboard()
    last_change = pasteboard.changeCount()
    runner.arm(cwd)
    print("clip-harness: iTerm2 watch armed")
    busy = False
    while True:
        await asyncio.sleep(0.4)
        if busy or pasteboard.changeCount() == last_change:
            continue
        last_change = pasteboard.changeCount()
        text = subprocess.run(["pbpaste"], capture_output=True,
                              timeout=5).stdout.decode("utf-8", "replace")
        if not text:
            continue
        busy = True
        try:
            outcome = await asyncio.to_thread(
                runner.process_text, text, cwd)
            if outcome is not None:
                print(f"clip-harness: {outcome.state.value} — "
                      f"{outcome.message}")
            last_change = pasteboard.changeCount()
        finally:
            busy = False


def run_iterm2_watch(settings, state_dir: Path, cwd: str) -> None:
    import iterm2
    iterm2.run_until_complete(
        lambda connection: main(connection, settings, state_dir, cwd))
