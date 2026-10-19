"""Local watch loop — clipboard polling without iTerm2.

Same semantics as the original loop (its README): poll the clipboard,
and when the text CHANGES to something the parser recognises (a
payload fence or a cancel/rerun trigger), hand it to the lifecycle
runner. Result blocks, denial texts and pending notes don't parse as
payloads, so the loop never re-triggers on its own output.
"""

from __future__ import annotations

import time
from pathlib import Path

from ..runner import LifecycleRunner


class WatchLoop:
    def __init__(self, runner: LifecycleRunner, clipboard,
                 cwd: Path, poll_s: float = 0.4, out=print):
        self.runner = runner
        self.clipboard = clipboard
        self.cwd = cwd
        self.poll_s = poll_s
        self.out = out
        self._last_seen: str | None = None

    def run_forever(self) -> None:
        self.runner.arm(self.cwd)
        self.out("clip-harness watch: armed — copy a ```shell payload "
                 "to run it (Ctrl-C to stop)")
        try:
            while True:
                text = self.clipboard.get_text()
                if text is not None and text != self._last_seen:
                    self._last_seen = text
                    outcome = self.runner.process_text(text, self.cwd)
                    if outcome is not None:
                        self.out(f"[{outcome.run_id}] {outcome.state.value}"
                                 f" — {outcome.message}")
                        # Whatever the runner put back is the new seen.
                        self._last_seen = self.clipboard.get_text()
                time.sleep(self.poll_s)
        except KeyboardInterrupt:
            self.runner.session_end(self.cwd)
            self.out("clip-harness watch: stopped")
