"""Shipped defaults — installed by `clip-harness init`.

Notifications are HOOKS, not a core feature: the core emits lifecycle
events, and these default hook scripts are what post a macOS
notification on completion, failure, review-pending and session end.
Delete or replace them freely — the core doesn't know they exist.
"""

from __future__ import annotations

import json
from pathlib import Path

NOTIFY_HOOK = '''#!/usr/bin/env python3
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
            return s.replace("\\\\", "\\\\\\\\").replace('"', '\\\\"')
        script = f'display notification "{esc(message)}" with title "{esc(title)}"'
        subprocess.run(["osascript", "-e", script], check=False, timeout=5)
    else:
        state = os.environ.get(
            "CLIP_HARNESS_STATE",
            os.path.expanduser("~/.local/state/clip-harness"))
        os.makedirs(state, exist_ok=True)
        with open(os.path.join(state, "notifications.log"), "a") as fh:
            fh.write(f"{title}: {message}\\n")


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
'''

AUDIT_HOOK = '''#!/usr/bin/env python3
"""Default clip-harness audit hook (observational): appends every
hook input it receives to audit.log in the state dir. A starting
point for building your own PostRun/reporting hooks."""
import json
import os
import sys


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    state = os.environ.get(
        "CLIP_HARNESS_STATE",
        os.path.expanduser("~/.local/state/clip-harness"))
    os.makedirs(state, exist_ok=True)
    with open(os.path.join(state, "audit.log"), "a") as fh:
        fh.write(json.dumps(data) + "\\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

BLOCK_SECRETS_HOOK = '''#!/usr/bin/env python3
"""Example PreRun gate hook: deny commands that read secret files.
Shows the permissionDecision contract — exit 0 with a JSON decision
(the harness also accepts exit 2 as a hard block)."""
import json
import sys

SECRET_MARKERS = (".env", "id_rsa", ".aws/credentials", ".netrc")


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # the harness fails gate hooks closed on ITS side
    command = str((data.get("tool_input") or {}).get("command", ""))
    for marker in SECRET_MARKERS:
        if marker in command:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreRun",
                    "permissionDecision": "deny",
                    "permissionDecisionReason":
                        f"command touches a secret file ({marker})",
                }}))
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def write_defaults(config_dir: Path) -> list[Path]:
    """Write the default hooks + a starter settings.json (never
    overwriting an existing settings.json). Returns written paths."""
    config_dir = Path(config_dir).expanduser()
    hooks_dir = config_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, source in (("notify.py", NOTIFY_HOOK),
                         ("audit_log.py", AUDIT_HOOK),
                         ("block_secrets.py", BLOCK_SECRETS_HOOK)):
        path = hooks_dir / name
        path.write_text(source, encoding="utf-8")
        path.chmod(0o755)
        written.append(path)
    settings_path = config_dir / "settings.json"
    if not settings_path.exists():
        notify = f"python3 {hooks_dir}/notify.py"
        settings = {
            "hooks": {
                "PostRun": [{"matcher": "*", "command": notify}],
                "PostRunFailure": [{"matcher": "*", "command": notify}],
                "ReviewDecision": [{"matcher": "*", "command": notify}],
                "SessionEnd": [{"matcher": "*", "command": notify}],
            }
        }
        settings_path.write_text(json.dumps(settings, indent=2),
                                 encoding="utf-8")
        written.append(settings_path)
    return written
