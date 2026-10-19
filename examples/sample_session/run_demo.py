#!/usr/bin/env python3
"""Sample session — the full lifecycle, headless.

An agent (or Ali) plays the Copilot role: it writes payloads to a
file-backed clipboard and a lifecycle runner — wired exactly like the
production one, except the clipboard is a file, the reviewer is a
thread that drops decision.json files, and the shipped default notify
hook logs to a file (no osascript on Linux) — executes them:

  1. a session-bound shell command
  2. a mutation payload (one UPDATE + one NEW) — ACCEPTED in review
  3. a mutation payload (a DELETE + an UPDATE) — REJECTED in review
  4. a bare unbound command (no fence at all)

It prints every result block and the full receipt ledger at the end.

Run from a checkout:  python3 examples/sample_session/run_demo.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from clip_harness.adapters.headless import FileClipboard, FileDropDecider
from clip_harness.defaults import NOTIFY_HOOK
from clip_harness.goals import GoalStore
from clip_harness.runner import LifecycleRunner
from clip_harness.settings import HookHandler, Settings

SESSION = "demo-session"


def reviewer(state_dir: Path, decisions: list[dict]) -> threading.Thread:
    """Plays the human: when a candidate appears, drop the next queued
    decision.json into its directory — the same file the real flow
    reads, with the same schema."""
    queue = list(decisions)

    def _review():
        seen: set[str] = set()
        deadline = time.time() + 30
        while queue and time.time() < deadline:
            for cand in sorted((state_dir / "candidates").glob("*")):
                if cand.name in seen or \
                        not (cand / "manifest.json").exists():
                    continue
                seen.add(cand.name)
                decision = queue.pop(0)
                time.sleep(0.4)  # the human glances at the diff first
                (cand / "decision.json").write_text(
                    json.dumps(decision), encoding="utf-8")
            time.sleep(0.1)

    thread = threading.Thread(target=_review, daemon=True)
    thread.start()
    return thread


def copilot(clipboard: FileClipboard, text: str) -> None:
    print("\n" + "=" * 68)
    print("COPILOT PUTS ON THE CLIPBOARD:")
    print(text)
    clipboard.set_text(text)


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="clip-harness-demo-"))
    project = root / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.py").write_text("print('v1')\n")
    (project / "README.md").write_text("# scratch project\n")
    state_dir = root / "state"
    os.environ["CLIP_HARNESS_STATE"] = str(state_dir)

    # The shipped default notify hook, wired like `clip-harness init`
    # would wire it — notifications are hooks, so the demo gets them
    # the same way production does.
    hooks_dir = root / "hooks"
    hooks_dir.mkdir()
    (hooks_dir / "notify.py").write_text(NOTIFY_HOOK, encoding="utf-8")
    notify = f"{sys.executable} {hooks_dir / 'notify.py'}"
    settings = Settings(
        state_dir=state_dir,
        hooks={event: [HookHandler(command=notify)]
               for event in ("PostRun", "PostRunFailure",
                             "ReviewDecision", "SessionEnd")})

    # Core goal/task state for the session (the nag's data).
    goals = GoalStore(state_dir, SESSION)
    goals.set_goal("Harden the scratch project")
    goals.add_task("Bump app.py to v2")
    goals.add_task("Add the feature module")

    clipboard = FileClipboard(root / "clipboard.txt")
    runner = LifecycleRunner(
        settings=settings, clipboard=clipboard,
        decider=FileDropDecider(timeout_s=15),
        presenter=lambda summary: print("\nMANIFEST PRESENTED FIRST:\n"
                                        + summary))

    print(f"demo workspace: {root}")

    reviews = reviewer(state_dir, [
        {"decision": "accept", "reason": "", "accepted_files": None},
        {"decision": "reject", "reason": "do not delete the README",
         "accepted_files": None},
    ])

    # 1 — a bound shell command.
    copilot(clipboard,
            f"```shell\nsession: {SESSION}\nls src && cat src/app.py\n```")
    outcome = runner.process_text(clipboard.get_text(), str(project),
                                  allow_bare=True)
    print("\nRESULT BLOCK:\n" + (outcome.result_block or outcome.message))
    assert outcome.state.value == "completed"

    # 2 — a mutation payload, accepted in review. (A header-only
    # shell fence binds the run to the session without commands.)
    copilot(clipboard,
            f"```shell\nsession: {SESSION}\n```\n"
            "```file\npath: src/app.py\nop: update\nprint('v2')\n```\n"
            "```file\npath: src/feature.py\nop: create\n"
            "print('feature')\n```")
    outcome = runner.process_text(clipboard.get_text(), str(project),
                                  allow_bare=True)
    print("\nRESULT BLOCK:\n" + (outcome.result_block or outcome.message))
    assert outcome.state.value == "completed", outcome
    assert (project / "src" / "app.py").read_text() == "print('v2')\n"
    assert (project / "src" / "feature.py").exists()

    # 3 — a mutation payload, rejected in review.
    copilot(clipboard,
            f"```shell\nsession: {SESSION}\n```\n"
            "```file\npath: README.md\nop: delete\n```\n"
            "```file\npath: src/app.py\nop: update\nprint('v3')\n```")
    outcome = runner.process_text(clipboard.get_text(), str(project),
                                  allow_bare=True)
    print("\nCLIPBOARD AFTER REJECTION:\n" + clipboard.get_text())
    assert outcome.state.value == "rejected"
    assert (project / "README.md").exists()               # untouched
    assert (project / "src" / "app.py").read_text() == "print('v2')\n"

    # 4 — a bare unbound command: no fence, no session, no goal nag.
    copilot(clipboard, 'echo "hello world"')
    outcome = runner.process_text(clipboard.get_text(), str(project),
                                  allow_bare=True)
    print("\nRESULT BLOCK:\n" + (outcome.result_block or outcome.message))
    assert outcome.state.value == "completed"
    assert "— goal:" not in outcome.result_block

    runner.session_end(str(project))
    reviews.join(timeout=5)

    print("\n" + "=" * 68)
    print("RECEIPTS (receipts.jsonl):")
    print((state_dir / "receipts.jsonl").read_text())
    notes = state_dir / "notifications.log"
    if notes.exists():
        print("NOTIFICATIONS (posted by the default notify HOOK):")
        print(notes.read_text())
    print("DEMO OK — full lifecycle ran headlessly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
