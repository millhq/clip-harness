#!/usr/bin/env python3
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
        fh.write(json.dumps(data) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
