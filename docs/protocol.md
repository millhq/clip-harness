# The payload protocol

> **This document is the single source of truth for the protocol.**
> [AGENTS.md](https://github.com/millhq/clip-harness/blob/main/AGENTS.md) —
> the instructions you give the chat model — is derived from it;
> keep the two in sync (a SKILL.md rendering can be generated from
> the same source later).

Detection is a ladder — the fence/metadata layer is progressive
enhancement over "whatever is on the clipboard runs", never a
requirement for the simple case:

1. **```shell fence** — commands with a metadata header.
2. **```file / ```patch / ```edit fences** — proposed file changes.
3. **Unfenced contents beginning with a metadata header** — the
   shell fence's contents standing alone, when the fence was lost
   in copying (see "When the fence is lost in copying", below).
4. **Unfenced contents beginning with a `path:` header** — the
   ```file / ```patch / ```edit fences' contents standing alone,
   under stricter recognition rules (same section, below).
5. **Bare clipboard text** — treated as a single unbound command
   (only on the explicit trigger path; the passive `watch` loop never
   auto-runs bare text).

## The ```shell fence

````
```shell
session: auth-refactor
action: run the tests
cwd: ~/work/api
timeout: 300
npm test
```
````

Leading `key: value` lines are the metadata header; the rest is shell
text. Recognised keys:

- `session:` — binds the run to that session id: addressable for
  cancel/rerun, receipts keyed by it, and its result blocks carry the
  goal/task standing section and capability-manifest refreshes.
  **No `session:` → the run is unbound but still runs.** A missing id
  never blocks execution.
- `action:` — `run` (default); the core goal/task operations
  `goal-set`, `goal-status`, `task-add`, `task-update` (these
  require a session, or a `goal:` header) and the read-only
  `goal-list` / `goal-show` (no session needed). `task-update` is
  the only status-change form: its body is one
  `<status>: <exact task title>` line per transition (status
  `done` / `doing` / `open`), and the batch applies atomically —
  every title is validated first, and an unknown, duplicated or
  conflicting title rejects the whole batch with nothing applied.
  (Migration: the single-task `task-doing` / `task-done` /
  `task-open` actions were removed; a payload naming one gets a
  static migration error pointing at `task-update`.)
- `cwd:` — working directory override.
- `timeout:` — per-command timeout in seconds.
- `attend:` — `true` opts this run into attended mode (below);
  anything else, or absent, is async.
- `writes:` — comma-separated paths the command declares it may
  write (relative entries resolve against the effective cwd). The
  declaration scopes the sandbox when the command is held for
  review (see [Reviewed shell commands](hooks.md#reviewed-shell-commands));
  an empty value is
  malformed and fails closed.
- `goal:` — a durable goal's `goal_uid`: binds the payload to that
  goal (its footer projects from the goal's record; goal/task
  actions target it). An unknown uid is refused — discover goals
  with `action: goal-list`. See [Durable goals](goals.md).
- `expect-revision:` — the compare-and-swap guard: an integer. A
  goal mutation proceeds only while the goal's current revision
  equals it (a stale writer is rejected, naming both revisions);
  on a plain run bound to a goal it asserts the revision, and a
  mismatch denies the run. A non-integer value is malformed.
- `action-id:` — an idempotency key (a uuid) for the whole
  payload. If the id was already applied, the payload is rejected
  as a duplicate — before anything executes or mutates — with the
  original outcome named. An empty value is malformed.

(```run is accepted as a legacy alias for ```shell.)

## When the fence is lost in copying

The canonical form is the fence — models emit fences, always. But
some chat UIs strip the outer fence when the human copies with the
block's copy button (Microsoft 365 Copilot does this), leaving the
block's contents on the clipboard. The harness therefore also
recognises those contents standing alone: text that **begins** with
a metadata header — a contiguous run of recognised `key:` lines from
line 1 — parses as that shell payload. The header is consumed as
metadata and never passed to the shell; the remaining lines are the
command, and goal/task `action:` payloads work identically. Bound
runs get the same manifest and sticky footer as fenced runs.

The mutation fences get the same tolerance, under stricter rules.
Text whose first line is an exact `path:` header is the ```file,
```patch or ```edit fence's contents when what follows
disambiguates it: an explicit `op:` line makes it a file payload —
file.write **requires** that op in this form, and the unfenced form
is stricter than the fence: the op must be `create`, `update` or
`delete` (`auto` is fenced-only), create/update require the file
content as a non-empty body, and delete requires an empty body. A
body beginning with a unified-diff header (`---` then `+++`) makes
it a patch payload. (The op wins if both are present: a file
whose content is itself a diff is legitimate.) A following
`replace_all:` line or an exact `old_string:` line makes it an
edit payload, parsed with the same strictness as the fence — a
malformed edit fails closed here too, never falling through to
bare shell text. The payload then flows through the identical
staging, review and apply path as a fenced one — same manifest,
same base-hash checks, same traversal rejection at staging.

This is a tolerance for a copying artifact, not a second format:

- Text that does not begin with a recognised metadata line is not
  an unfenced payload — it is bare text (rung 5), as before. An
  unrecognised first line (e.g. `foo: bar`) starts no header.
- A recognised key with an invalid value inside the header
  (`timeout: soon`, `attend: maybe`) is malformed metadata: the
  payload is rejected and nothing executes — it never falls back
  to running the header as shell text.
- A metadata line appearing after the command body has begun is
  ordinary shell text, never re-parsed.
- Metadata with no command body is rejected.
- A `path:` header followed by neither a valid `op:` line nor a
  diff header is malformed: rejected, never treated as bare shell
  text, never staged. An `op:` line with any other value is
  malformed the same way. A `path:` line appearing after a shell
  body has begun is ordinary shell text.

### Execution modes

Wherever a human is in the loop, the review is taken in the
terminal — and plain runs are fire-and-forget: the human is never
blocked waiting on output.

- **Trigger / `run` (interactive).** Firing the trigger (or
  `clip-harness run`) puts you in the loop: a payload with file
  changes is staged, the review surface (manifest, then inline
  diffs) is presented in the terminal, and the decision is taken
  in the same invocation — accept, accept-edited, reject, or
  *defer*. The prompt also offers a VS Code view of the candidate;
  it opens only when you pick it — and in the editor, saving is
  the decision (see the [review gate](hooks.md#the-review-gate-73-flow)).
  This holds for attended payloads and pure file/patch payloads
  alike. Plain commands just run.
- **Live run visibility (trigger / `run`, at a terminal).** While
  a command executes, the terminal shows a header — the command,
  its cwd, the bound session, the timeout — and then the output
  itself as it is produced, with a `… running Ns` heartbeat when
  the command goes quiet; the run's `output.txt` fills as it goes,
  so `tail -f` works mid-run too. This is a view, not a second
  artifact: the result block still arrives complete at the end,
  byte-for-byte what a silent run produces, and the clipboard,
  receipts and files never see the header or the heartbeat. The
  watch loops and non-terminal invocations don't stream.
- **Parked (deferred / watch).** Deferring at the trigger prompt —
  or any payload with file changes under the `watch` loops, which
  never prompt — **parks** the run: the candidate is staged, the
  `ReviewDecision` *pending* notification fires, and the run waits
  in `pending_review`. The decision arrives later (`candidate
  review` / `decide` / `apply`, or a `decision.json` drop); only
  then do apply, receipt, clipboard, and the completion
  notification happen. For a parked review, rejecting *is* the
  steering. A held shell command parks the same way, as a command
  candidate (see [Reviewed shell commands](hooks.md#reviewed-shell-commands)).
- **Attended (`attend: true`).** The payload itself demands the
  inline wait in any context that has a decision window — e.g.
  the iTerm2 watch's `decision.json` drop — for the runs where the
  human wants to sit in the loop even where the entry point would
  otherwise park. `cancel` steers a running command in every mode.

### File fences — the mutation branch

One copied message may carry several fences: one ```shell fence plus
one fence per file. A file fence carries the FULL content — the human
reviews the actual bytes as an inline diff in the terminal (below),
never a description in the chat:

````
```file
path: src/auth.py
op: update
<full file content>
```

```patch
path: src/tokens.py
<unified diff>
```
````

`op:` is optional (`create`/`update`/`delete`); the gate classifies
each file against disk — **NEW / UPDATE / DELETE / NO-CHANGE** — and
flags declared-vs-actual mismatches. File fences with no shell fence
are a pure mutation payload; a header-only shell fence binds one to a
session. For ```patch fences, `path:` is the authoritative target:
the diff's `---`/`+++` header filenames are checked against it and
normalized to it before application, whatever shape they arrive in.

### The ```edit fence — exact-text edits

For a change to an existing file, prefer ```edit over ```patch:
the model supplies exact text instead of a hand-counted diff, and
the harness computes the candidate bytes and generates the review
diff itself:

````
```edit
path: src/auth.py
replace_all: false
old_string:
def old():
    return 1
new_string:
def new():
    return 2
```
````

The rules mirror Claude Code's Edit tool, which this maps onto:

- `path:` is required and first — the authoritative target,
  exactly like the other mutation fences.
- `replace_all:` is an optional header line (Claude Code's exact
  parameter name); only the literal values `true` / `false` are
  valid — anything else is malformed and fails closed. Default
  `false`.
- The sections are delimited by lines that are exactly
  `old_string:` and then exactly `new_string:` — the first
  `new_string:` line after `old_string:` is the delimiter. A
  section's text is its lines joined with newlines, with no
  trailing newline.
- Matching is exact, whitespace included, against the live file
  (which must decode as UTF-8). The gate fails the staging when
  `old_string` occurs zero times (the remedy: copy the exact
  current text, whitespace included, and re-send), or more than
  once without `replace_all: true` (the remedies: include more
  surrounding context to make it unique, or set `replace_all`).
  `old_string` equal to `new_string` fails too — there is nothing
  to change.
- An empty `new_string:` section is valid: it **deletes** the
  matched block. An **insert** is expressed inside the same one
  operation — `new_string` carries the anchor text plus the new
  text. There are no other edit operations.
- An empty `old_string`, a missing section marker, or an `op:`
  line is malformed and fails closed. An edit **never creates a
  file** — its target must already exist; creation is the ```file
  fence's job, and the staging error says so.
- **One edit per file per payload:** a second spec for the same
  path where at least one is an edit is rejected at staging. Like
  sequential Edit calls, the next edit goes in the next payload,
  after the result; a multi-part change to one file travels as a
  single ```file fence or a single valid ```patch.
- If the text being edited itself contains a literal line that is
  exactly `old_string:` or `new_string:`, the section markers
  collide — send that change as a ```file fence instead.
- ```patch stays for pre-generated, known-valid diffs (produced
  by a tool, not hand-written). A patch whose @@ header counts
  don't match its body fails staging with a diagnostic naming,
  per offending hunk, the declared vs actual old/new counts —
  and the error points back here: fix the @@ counts, or re-send
  the change as an ```edit fence.

### Control triggers

Single-line, as in the original loop: `` ```cancel``` `` /
`` ```cancel id=<session>``` `` and `` ```rerun``` `` /
`` ```rerun id=<session>``` ``.

### The result block

````
```result
$ npm test
<output — oversized output elided to a head + tail digest; the full output stays in a file on disk>
[exit 0 · 3.2s]
files:
  ✓ src/auth.py — UPDATED
    --- a/src/auth.py
    +++ b/src/auth.py
    @@ -10,3 +10,3 @@
     unchanged context
    -old line
    +new line
— capabilities:
  ...
[goal] Ship the auth refactor [active] (3f9a1c2e)
[open tasks] [doing] wire tokens · [open] update tests
```
````

**One artifact, two audiences.** The result block is a single shared
artifact: the text rendered in the terminal and the text on the
clipboard are the *same bytes* — what the human sees is exactly what
the model reads when it is pasted back. There is no prettier human
version and no separate machine version.

- The `files:` section appears when a mutation was decided, with
  NEW/UPDATED/DELETED tags (and ✗ lines for rejected entries). Each
  entry is followed by its **inline diff** — applied and rejected
  alike — so the paste-back shows the model its own proposal exactly
  as rendered and what happened to it. Diffs are capped per file
  (80 lines / 6000 chars) with a `… N more lines — full diff in
  <candidate dir>` pointer.
- **Sticky footer.** Every session-bound block ENDS with the same
  lines in the same position: `[goal] …` then `[open tasks] …`.
  The `[goal]` line is projected from the durable goal record —
  goal text, status, and the goal's short uid. There is no
  `[step]` line: nothing stores a "current step" — the task in
  progress shows as `[doing]` inside `[open tasks]`. The footer is
  **core-generated** (never hook-supplied) and appears in every
  bound block, hooks or no hooks, so both the human and the model
  are re-anchored on the goal and the open tasks every round trip.
  Hook `additionalContext`, capability-manifest refreshes, and
  pending state all sit *above* the footer; the footer is always
  last. Unbound runs carry neither the footer nor manifest
  refreshes. One more line may sit immediately above the footer
  (or end an unbound block): the `[update]` notice, when a newer
  release is available (see [Updating](cli.md#updating)).
- A rejection puts plain rejection text on the clipboard instead
  (the rejected diffs included, marked rejected, with the reason),
  ready to paste back so the model can re-propose.

### Oversized output

A command's full output is always preserved byte-for-byte at
`<state_dir>/runs/<run_id>/output.txt`. What goes into the result
block is bounded by the `result_budget_chars` setting (default
100,000): output past the budget is elided to a head + tail digest
around one marker line —

`[output elided: N of T chars shown · full output (T chars) saved at <path> · page it with: sed -n 'A,Bp' <path> · grep <path> · tail -c E <path>]`

The default leaves room inside a 128k-capped chat message (Microsoft
365 Copilot's per-message limit) for your own words and the block's
footer; set the budget for whatever surface you paste into. Nothing
is lost and no re-run is needed: the model pages the stored file with
ordinary shell commands in a follow-up payload, so the bulk never
occupies its context — it stays addressable instead.

## Capability manifest

The manifest is the discovery mechanism by which the chat model gains
capabilities over the clipboard transport: a compact list of the
tools it may invoke, each with the block shape that invokes it
(`shell.run`, `file.write`, `file.edit`, `patch.apply`,
`session.cancel`, `session.rerun`, `goal.set`, `goal.list`,
`goal.show`, `task.add`, `task.update`, …). The core
generates it for the built-ins; settings may declare more under
`"tools"`; and **hooks may register additional tools at runtime** via
`registeredTools` in their output — a registered tool appears in the
manifest from then on, refreshed into the next bound result block.
