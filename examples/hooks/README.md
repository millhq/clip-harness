# Example hooks

These are the same scripts `clip-harness init` installs into
`~/.config/clip-harness/hooks/`:

- **notify.py** — the default notification hook. Notifications are
  hooks in clip-harness, never core: this one posts a macOS
  notification (osascript) on PostRun / PostRunFailure /
  ReviewDecision-pending / SessionEnd, and falls back to a
  `notifications.log` file elsewhere.
- **audit_log.py** — appends every hook input it receives to
  `audit.log`; a starting point for reporting hooks.
- **block_secrets.py** — an example PreRun *gate* hook: denies
  commands that touch secret files via `permissionDecision`.
  Gate hooks fail closed — if this script crashes, the payload
  does not run.

See `../settings.example.json` for how they're wired, and the
README's "Hook lifecycle" section for the full contract.
