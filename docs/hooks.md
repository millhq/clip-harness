# Hook lifecycle

The lifecycle is modelled on Claude Code's hooks, mapped onto the
harness (one dispatcher, `clip-harness hook --event <Event>`, one JSON
object in on stdin, at most one JSON object out on stdout, diagnostics
to stderr):

| event | when | analogue |
|---|---|---|
| `SessionStart` | harness armed: ledger init, tool check, env/cwd snapshot | SessionStart |
| `PayloadCaptured` | clipboard captured: hash + timestamp + raw bytes receipted **first**; empty/oversized/non-text rejected | UserPromptSubmit |
| `PreRun` | the gate: parse/classify, allowlist/denylist, protected paths, destructive patterns → allow/deny/ask/rewrite | PreToolUse |
| `ReviewDecision` | the mutation branch: candidate staged, human decision captured as explicit data | PermissionRequest |
| `PostRun` | receipt with the *executed* input + response + duration — written **before** the clipboard is overwritten | PostToolUse |
| `PostRunFailure` | a **separate** event (not a flag): failure receipt; partial applies roll back from base-hash snapshots | PostToolUseFailure |
| `RunClosed` | refuses a “clean” close while a candidate decision is pending — the run is left in a recorded pending state | Stop |
| `SessionEnd` | ledger summary | SessionEnd |
| `ConfigChange` | allowlist/hook-config edits made while armed, receipted as their own events | ConfigChange |

## The contract

- Input fields: `session_id`, `run_id`, `hook_event_name`, `cwd`,
  `clipboard_sha256`, `tool_name` (`Bash` for commands; reclassified
  `Write`/`Edit`/`Delete` for the mutation branch), `tool_input`,
  `tool_use_id`; post events add `tool_response` or `error` +
  `duration_ms`. Both input and output are **validated against the
  schemas in `clip_harness/contract.py`**; a schema violation is a
  hook error.
- Decisions via `hookSpecificOutput`: `permissionDecision`
  (`allow`/`deny`/`ask`), `permissionDecisionReason`, `updatedInput`
  (replaces the *whole* input; the allowlist is re-checked against
  the rewritten input), `updatedToolOutput` (PostRun only — rewrites
  the clipboard copy; the receipt keeps the true output),
  `additionalContext`, `registeredTools`.
- Exit codes: `0` = proceed (the JSON may still deny/ask/rewrite);
  `2` = hard block, evaluated before the allowlist, stderr is the
  reason, and it wins over a JSON `allow`; any other exit, a crash, a
  timeout, or unparseable/schema-violating output = **hook error**.
- **The deliberate deviation:** gate hooks (`PayloadCaptured`,
  `PreRun`, `ReviewDecision`, `RunClosed`) **fail closed** — a gate
  hook error means the payload does *not* run, with the reason in the
  receipt (Claude Code's `onFailure: "block"` behaviour, made the
  default). Observational hooks fail open.
- Hooks are command handlers in a settings file (below), with Claude
  Code-style matchers on tool name (exact/`|`-lists, or regex), an
  optional `if` glob on the command text, and per-hook timeouts.
- A review that produces no decision in its window leaves the run
  **PENDING** with the candidate intact — never auto-applied, never
  silently discarded. Complete it later with
  `clip-harness candidate review <id>`.

## The review gate (§7.3 flow)

File payloads are never written live. The gate classifies each file
against disk, stages candidates with base hashes and a manifest, and
the review surface presents the **manifest first** (path · op · line
counts), **then each change as an inline unified diff** in the
terminal — a create renders as a full addition, a delete as the
removed content, an update as a compact diff with context lines,
each capped with a pointer to the candidate directory for the full
diff. That inline diff is the default review surface; VS Code
(`code --diff` per update, the candidate for creates, the live file
for deletes) remains available as the deep-review option for the
same staged candidate — offered inside the terminal prompt and
opened only when the human picks it; it never opens on its own.
On the trigger / `run` paths the surface is presented inline at
staging time and the decision is taken in the same invocation
(deferring parks the run); under the watch loops the run parks
and the surface is presented when the human comes to decide
(`candidate review` / `candidate show`); an `attend: true`
payload forces the inline wait wherever a decision window exists.
The
decision is captured as data (`decision.json`: `accept` /
`accept-edited` / `reject` + optional reason + optional per-file
`accepted_files`). Apply runs only on accept, applies only accepted
entries, verifies post-apply hashes, and rolls back from the
pre-apply snapshots on partial failure. On reject: no post hooks
fire, the receipt records the denial, and the clipboard gets the
rejection text (rejected diffs included).

Picking the VS Code view waits for the editor: the candidate
opens in a single `code --wait` session (manifest first, then the
diffs), and the contract is *save + close = apply your edits* —
if any staged copy's content differs when VS Code closes, the
review concludes on the spot as `accept-edited` and applies your
edited bytes, for the whole candidate (per-file picks remain a
terminal-prompt capability). Close without saving and nothing
was decided: you are back at the terminal prompt. A delete's
live file is view-only in the editor — changing it does not
count as an edit, and the gate's stale-base check still guards
the apply.

Built-in policy runs before hooks: hard-block patterns are evaluated
**before** the allowlist (exit-2 semantics); a denylist match denies;
a non-empty allowlist that doesn't match asks; shell commands that
look like mutations ask under `mutation_gate: "review"` (the default)
so file changes can travel as reviewable fences instead. An explicit
allowlist **hit** is the human's standing approval: it approves the
command outright (this is what a review's *always allow* persists —
below). A mutation the gate holds is not a dead end, though: the
command itself is reviewable, as the next section describes.

## Reviewed shell commands

Some commands cannot be predeclared as file fences — a supplier
script that *generates* its outputs, a build, a code generator.
When the gate's verdict on a shell command is **ask** (a mutation
under `mutation_gate: "review"`, or a command not on the allowlist),
the run pauses for a decision instead of executing, wherever the
command appears: bare, or as the shell part of a mixed payload
(after its files are accepted). The command is staged as a
**command candidate** in the same candidates store as file
candidates (manifest kind `"command"`: the command, cwd, timeout,
session, declared `writes:`, the gate's reason, and the clipboard
hash) and the review surface shows:

- the full command, its cwd, session (when bound) and timeout;
- the gate's reason for holding it;
- the declared write scope — or, with no `writes:` header, that
  writes are confined to the working directory + temp;
- the protected carve-outs (below) that no accept can override.

Decisions, at the terminal prompt or as recorded decision data:

- **accept** — execute under the sandbox (the default accept);
- **accept unsandboxed** — execute bare. Offered only when the
  `shell_sandbox` setting is `"optional"` (the default);
  `"strict"` removes it entirely, from the prompt and from recorded
  decisions alike;
- **always allow** — accept sandboxed AND persist an allowlist
  pattern derived from the command (first token + ` *`, shown in
  the prompt, e.g. `suppliers/openapi/discover *`) into the
  **project** settings (`.clip-harness/settings.json` under the
  cwd, other keys preserved; a ConfigChange is recorded), so future
  runs of that shape evaluate ALLOW and are not held;
- **reject** (+reason) — nothing runs; the denial result carries
  the reason back to the model;
- **later** — no decision: the run parks in `pending_review` as a
  command candidate, exactly like a file candidate. The clipboard
  note names the mechanism: `clip-harness candidate review <id>`.
  `candidate list` / `show` present it as a command with its scope
  and reason; `candidate review` offers the same decisions and
  executes an accepted command through the runner (PostRun hooks,
  bound footer, result block); `candidate review --decision`
  accepts `accept` (= sandboxed), `accept-unsandboxed` and
  `reject` for command candidates. `candidate decide` records a
  decision for a later `candidate apply`, as with files — one
  difference: for a command candidate, *apply* means **execute the
  approved command** (there are no files to write).

**The sandbox (macOS Seatbelt).** A sandboxed execution is wrapped
in `sandbox-exec -p <profile>` at process spawn; supervision,
timeout and cancel are unchanged. The profile is a pure function of
(cwd, declared writes, temp dirs, config dirs): reads are broadly
allowed; file writes are denied by default, then allowed for the
effective cwd, each declared `writes:` path, and the temp dirs;
hard write-denies that no allow overrides sit inside those roots —
`<cwd>/.git/hooks`, `<cwd>/.git/config`, the user's clip-harness
config dir, and `<cwd>/.clip-harness`. Process exec/fork is
allowed. Non-goals in this slice: **network is unrestricted** (no
network rules are emitted), and the sandbox is **macOS Seatbelt
only** — where `sandbox-exec` is unavailable, a sandboxed accept
**fails closed** with a clear result-block error and nothing runs;
the human's recourse is an explicit unsandboxed accept.

The result block of a reviewed run records the approval on the
command line — `$ make evidence (reviewed · sandboxed)` — the one
annotation a reviewed run adds. Hard blocks (destructive patterns,
protected-path mutations, the denylist) stay unreviewable denials,
and `mutation_gate: "deny"` stays a flat denial: neither can be
approved at review.

## Settings

User scope `~/.config/clip-harness/settings.json` + project scope
`.clip-harness/settings.json`, merged like Claude Code's: handler and
list settings concatenate (user first), scalars are overridden by the
project. See [examples/settings.example.json](https://github.com/millhq/clip-harness/blob/main/examples/settings.example.json).

```json
{
  "timeout": 120,
  "allowlist": ["git status", "ls *"],
  "denylist": ["sudo *"],
  "protected_paths": [".env", "~/.ssh"],
  "hard_block_patterns": ["rm\\s+-rf\\s+/"],
  "mutation_gate": "review",
  "shell_sandbox": "optional",
  "result_budget_chars": 100000,
  "color": "auto",
  "hooks": {
    "PreRun": [{"matcher": "Bash", "command": "~/hooks/check.py", "timeout": 10}],
    "PostRun": [{"matcher": "*", "command": "~/hooks/notify.py"}]
  }
}
```

State (receipts, sessions, candidates, and the durable goal
goal store) lives in `~/.local/state/clip-harness`
(override with `"state_dir"`).

**Terminal color.** Diffs printed to a terminal — the review
presentation, `candidate show`, printed result blocks — are colorized
at print time: additions green, deletions red, file/hunk headers
cyan. Color lives only in the rendering; the text itself (clipboard,
result blocks, receipts, files) always stays plain. Control it with
the `"color"` setting: `"auto"` (default — color only when stdout is
a TTY), `"always"`, or `"never"`. The `NO_COLOR` environment variable
forces color off regardless of the setting.
