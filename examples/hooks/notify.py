#!/usr/bin/env python3
"""Default clip-harness notification hook (observational, fail-open).

Reads one hook-input JSON object on stdin and posts a notification for
the events it cares about: PostRun (done), PostRunFailure (FAILED,
worded differently), ReviewDecision (review pending / rejected) and
SessionEnd. On macOS it shells out to osascript; elsewhere it appends
to notifications.log in the state dir so headless runs stay visible.
Prints nothing — an observational hook has no opinion.
"""
import json
import os
import shutil
import subprocess
import sys


def post(title: str, message: str) -> None:
    if shutil.which("osascript"):
        def esc(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')
        script = f'display notification "{esc(message)}" with title "{esc(title)}"'
        subprocess.run(["osascript", "-e", script], check=False, timeout=5)
    else:
        state = os.environ.get(
            "CLIP_HARNESS_STATE",
            os.path.expanduser("~/.local/state/clip-harness"))
        os.makedirs(state, exist_ok=True)
        with open(os.path.join(state, "notifications.log"), "a") as fh:
            fh.write(f"{title}: {message}\n")


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    event = data.get("hook_event_name", "")
    if event == "PostRun":
        resp = data.get("tool_response") or {}
        ms = data.get("duration_ms") or 0
        post("clip-harness: done",
             f"{resp.get('exit_desc', '0')} · {ms / 1000:.1f}s")
    elif event == "PostRunFailure":
        post("clip-harness: run FAILED",
             str(data.get("error") or "failed"))
    elif event == "ReviewDecision":
        status = data.get("review_status", "")
        if status == "pending":
            post("clip-harness: review pending",
                 "a candidate is staged and waiting for your decision")
        elif status == "reject":
            decision = data.get("decision") or {}
            post("clip-harness: changes rejected",
                 str(decision.get("reason") or ""))
    elif event == "SessionEnd":
        post("clip-harness", "session closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
