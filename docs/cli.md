# CLI surface

```
clip-harness                      # zero-arg trigger: clipboard -> run -> clipboard
clip-harness watch [--backend local|iterm2]
clip-harness run (--clipboard|--stdin|--file F|--command C) [--session ID]
clip-harness hook --event PreRun  # the hook dispatcher (JSON in/out)
clip-harness init                 # default hooks + starter settings
clip-harness goal --session ID set|show|status|task-add|task-update|task-list
clip-harness goal list|show UID|export [UID|--all]|import FILE
clip-harness candidate list|show|review|decide|apply [ID]
clip-harness sessions             # bound sessions
clip-harness cancel [ID]          # bound id, or the last run
clip-harness rerun [ID]
clip-harness finish [--run ID|--session ID] [--copy]
clip-harness receipt [--run ID] [--tail N] [--summary]
clip-harness version [--check]    # --check: a fresh update check
clip-harness upgrade              # verified self-upgrade (see Updating)
```

Headless use (tests, demos, CI): set `CLIP_HARNESS_CLIPBOARD_FILE` to
use a file as the clipboard, and wire hook commands that log instead
of notifying. `examples/sample_session/run_demo.py` runs the full
lifecycle this way.

## Updating

The harness never updates itself silently — a tool whose job is
reviewing what executes does not change underneath a session. The
pattern is the converged dev-tool one (gh, npm, rustup): notify
in-band, upgrade on one explicit, verified command.

- **The notice rides the result blocks.** At most once a day the
  harness checks the release feed — GitHub Releases'
  `/releases/latest/download/SHA256SUMS`, no token, no account —
  and caches the verdict in `<state_dir>/update-check.json`. When a
  newer release exists, result blocks gain one line immediately
  above the sticky footer (or at the end of an unbound block):

  `[update] clip-harness 0.3.0 is available (you run 0.2.1) — upgrade with: clip-harness upgrade`

  The model reading the block back sees it too, so it can tell you
  you're behind. An unreachable feed means no line and no error:
  the check never fails, delays, or otherwise touches a run.
- **`clip-harness upgrade`** does the swap. It reads the latest
  release's SHA256SUMS, downloads the wheel, and **verifies its
  sha256 before installing anything** — a mismatch, or a feed
  naming no wheel, aborts with nothing installed. The verified
  wheel installs into the current environment with the current
  interpreter (the pipx venv the harness itself runs in), which
  also regenerates the `clip` alias; the new version takes effect
  on the next run. Already current: it says so and exits 0. If pip
  is unavailable in the environment it prints the equivalent
  manual `pipx` commands instead.
- **`clip-harness version --check`** forces a fresh check on
  demand, ignoring the daily throttle, and prints the verdict.

`CLIP_HARNESS_UPDATE_BASE` overrides the feed base URL — mirror
support, and how the test suites point the whole mechanism at a
local `file://` directory feed.
