#!/usr/bin/env python3
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
