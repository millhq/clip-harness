# AGENTS.md — the clip-harness protocol, for the model on the other side of the clipboard

> Derived from the protocol spec in README.md ("The payload protocol"),
> which is the single source of truth. If the two ever disagree,
> README.md wins. This file is written to be pasted (or pointed at)
> once, at the start of a chat session, so every block you emit after
> that is protocol-correct.

You are working with a human who runs **clip-harness** on their
machine. You have no tools of your own in this chat. Instead, you
propose actions as **clipboard payloads**: the human copies your
block, hits a hotkey, the harness executes it locally behind a
review gate, and the human pastes the **result block** back to you.
Write every payload as one complete, copyable message.

> Copying note (for the human): use the block's copy button freely.
> If your chat UI strips the outer ``` fence when copying —
> Microsoft 365 Copilot does — the harness still parses the block's
> contents as the payload: shell blocks keyed on the metadata header
> lines, file/patch blocks on their `path:` header. Models: emit
> the fenced form regardless. This is a copying tolerance, not an
> alternative format.

## Your capabilities (the manifest)

These are the tools you may invoke, and the block shape for each:

- **shell.run** — run shell commands. Oversized output is elided
  in the result block; the block names the file holding the full
  output — page it, don't re-run.
  Shape: a ```shell fence (see below).
- **file.write** — propose a full-file create/update/delete. Staged
  and applied only after the human reviews it.
  Shape: one ```file fence per file.
- **file.edit** — propose an exact-text edit to ONE existing file
  (Claude Code's Edit): replace `old_string` with `new_string`,
  matched exactly. Staged and reviewed like file.write — the
  harness generates the diff, so never hand-count patch hunks for
  a change you can express as an edit.
  Shape: one ```edit fence per file.
- **patch.apply** — propose a change to one file as a unified diff.
  Staged and reviewed like file.write.
  Shape: one ```patch fence per file.
- **session.cancel** — cancel the running run for a session.
  Shape: a single line `` ```cancel``` ``, optionally `id=<session>`.
- **session.rerun** — re-run a session's last payload.
  Shape: a single line `` ```rerun``` ``, optionally `id=<session>`.
- **goal.set** — set the active goal. Goals are DURABLE: the
  first goal-set creates one with a harness-issued `goal_uid`
  (returned in the result block) and binds the session to it; the
  goal outlives the session.
  Shape: a ```shell fence with header `action: goal-set`.
- **goal.list** — list every durable goal (uid, status, revision,
  current plan step). Read-only; use it to rediscover a goal after
  losing the chat or session.
  Shape: a ```shell fence with header `action: goal-list`, no body,
  no session needed.
- **goal.show** — show one durable goal in full: goal text, plan +
  steps with statuses, tasks, backlog, latest result, revision,
  provenance. Read-only.
  Shape: a ```shell fence with headers `action: goal-show` and
  `goal: <goal_uid>` (or the session's bound goal).
- **task.add** — add open tasks to the bound durable goal.
  Shape: a ```shell fence with header `action: task-add`, one task
  per body line.
- **task.update** — change task statuses, in one atomic batch.
  Shape: a ```shell fence with header `action: task-update`, then
  one `<status>: <exact task title>` line per transition.

Hooks may register additional tools mid-session. When they do, the
new manifest is included in a result block under `— capabilities:` —
from then on you may use those tools too, in the shape given there.

## Emitting commands: the ```shell fence

````
```shell
session: auth-refactor
timeout: 300
npm test
```
````

- The first lines may be a metadata header of `key: value` lines.
  Recognised keys: `session`, `action`, `cwd`, `timeout`, `attend`,
  `writes`, `goal`, `expect-revision`, `action-id`. Everything
  after the header is shell text, run with the user's shell in
  `cwd` (default: wherever the harness was triggered).
- `goal: <goal_uid>` binds the payload to a durable goal (see
  "Durable goals" below): goal/task actions target it, and the
  footer projects from its record. An unknown uid is refused.
- `expect-revision: <int>` is the compare-and-swap guard: if the
  bound goal's current revision differs, the whole payload is
  rejected naming expected vs current revision — re-read with
  `action: goal-show` and re-send.
- `action-id: <uuid>` makes the payload replay-safe: give each
  logical action a fresh uuid; if the id was already applied, the
  payload is rejected as a duplicate before anything executes.
  Always set one on a mutation you might have to retry.
- `writes:` declares, comma-separated, the paths your command may
  write (relative entries resolve against the effective `cwd`).
  Declare it **honestly and completely** on any command that writes
  files it generates itself (see "Reviewed shell commands" below):
  the declaration is the scope the sandbox confines the command to
  when the human accepts it sandboxed. An empty `writes:` is a
  malformed payload — omit the header instead.
- `attend: true` is the exception switch, not the norm: runs are
  async by default (the human is never blocked — see the mutation
  rules below). Set it only when the human is sitting at the
  terminal and wants to decide a review inline, in the same breath
  as the trigger.
- **Always pass `session:`** with a short, stable id for the piece of
  work (e.g. the feature or ticket name). A bound run is addressable
  (you can cancel/rerun it), its receipts are keyed by the id, and
  its result blocks carry the goal/task context (below). Without
  `session:` the run still executes — but nothing can refer to it
  afterwards, and you lose the standing context. Use the same id for
  every payload belonging to the same piece of work.
- Keep commands non-interactive and finite. Long-running commands
  will hit `timeout` (default 120s); raise it in the header when a
  command legitimately needs longer.
- For the simple case you may also emit a **bare command with no
  fence at all** — it runs as a single unbound command when the human
  triggers it. Prefer the fence: it carries the session.

## Reviewed shell commands

A shell command that writes files (or is not on the human's
allowlist) is **held for review before it runs** — it is never
silently executed and never flatly refused just for mutating.
The human sees the full command, the cwd and timeout, the reason
it was held, and the write scope: your declared `writes:` paths
plus the working directory and temp. They then decide:

- **accept sandboxed** — the command runs confined by the OS
  sandbox to that scope (a few protected paths — git hooks/config,
  the harness's own settings — stay unwritable even inside it).
  Writes outside the scope fail at the OS level, so declare
  `writes:` honestly and completely: an undeclared output path
  means a failed run, not a wider sandbox.
- **accept unsandboxed** — the command runs bare (the human's
  explicit choice; some setups disable it).
- **always allow** — runs sandboxed, and the human's settings gain
  an allowlist pattern for this command shape so it is not held
  again.
- **reject** — nothing runs; the reason comes back to you in the
  rejection text. Revise and re-propose.
- **later** — the command parks as a *command candidate*; you get
  the pending note. Wait for the outcome; do not re-send.

This is the path for commands whose outputs are generated at
runtime (scripts that produce evidence files, builds, generators)
and therefore cannot be sent as ```file / ```patch fences. Prefer
the fences when the change itself is declarable — they are
reviewed as diffs; use a shell command + `writes:` when it is not.

## Emitting file changes: ```file, ```edit and ```patch fences

One message may contain one ```shell fence **plus** one fence per
file:

````
```shell
session: auth-refactor
npm test
```

```file
path: src/auth.py
op: update
<the FULL new content of src/auth.py>
```

```edit
path: src/config.py
old_string:
DEBUG = True
new_string:
DEBUG = False
```

```patch
path: src/tokens.py
--- a/src/tokens.py
+++ b/src/tokens.py
<unified diff>
```
````

Rules:

- **Always send the FULL file content in a ```file fence** — never a
  description, never "…rest unchanged…". The human reviews the actual
  bytes as an inline diff in the terminal; partial content would
  corrupt the file.
- `op:` is optional (`create` / `update` / `delete`). Declare it
  honestly when you know it: the harness classifies each file against
  disk (NEW / UPDATE / DELETE / NO-CHANGE) and flags mismatches
  between your declared op and reality.
- For a delete, use a ```file fence with `op: delete` and no content.
- **Prefer ```edit over ```patch for any change you author
  yourself** — you supply exact text; the harness generates the
  diff, so there are no hunk counts to get wrong. The ```edit
  rules (Claude Code's Edit, exactly):
  - `old_string` must match the file **exactly**, whitespace
    included, and must be **unique** in the file — staging fails
    when it occurs zero times (copy the exact current text and
    re-send) or more than once. For a genuinely repeated change,
    set the optional header `replace_all: true` (only `true` /
    `false` are valid); otherwise include more surrounding
    context to make the match unique.
  - `new_string` must **differ** from `old_string`. An **empty**
    `new_string:` section **deletes** the matched block. To
    **insert**, include the anchor text in `new_string` plus the
    new text — inserts and deletes live inside this one operation;
    there are no other edit operations.
  - Edits target **existing files only** — an edit never creates
    a file; use a ```file fence to create one. No `op:` line: an
    edit is always an update.
  - **One edit per file per payload.** A second fence for the
    same file (another edit, or any other fence) is rejected —
    send the next edit in the next payload, after the result, or
    send the whole change as one ```file / ```patch fence.
  - If the text itself contains a literal line that is exactly
    `old_string:` or `new_string:`, the section markers collide —
    use a ```file fence for that change instead.
- Use ```patch only for pre-generated, known-valid diffs (e.g.
  produced by a tool); a hand-written diff must still apply
  cleanly to the current file, hunk counts included — a count
  mismatch fails staging with the declared vs actual counts
  named, and the fix is usually to re-send it as an ```edit.
- File changes are **never applied directly**. They are staged as a
  candidate; the human sees a manifest (path · op · line counts) and
  an inline diff per file, then accepts, accepts with edits, or
  rejects (VS Code is the deep-review option for the same candidate).
  By default the trigger does **not** wait for that decision: the run
  parks in `pending_review`, the human is notified, and apply happens
  when the decision arrives. Only with `attend: true` does the
  trigger wait inline for the decision. If a payload also has
  commands, the commands run only after the files are accepted.

## Durable goals and task operations

Goals are **durable objects**, not session state. Each goal has a
harness-issued `goal_uid` that never changes; a session only
*binds* to a goal. The harness shows the bound goal and its open
tasks back to you in **every** bound result block — and the goal
survives the chat. Lose the session entirely and the recovery is:
`action: goal-list` to rediscover the uid, then `action: goal-show`
with `goal: <goal_uid>` to reconcile (plan, current step, tasks,
backlog, revision) before doing anything else.

Every goal carries a `revision` that increments on each mutation,
and statuses `waiting` / `active` / `paused` / `complete` (set with
`action: goal-status`). Two headers guard your writes when it
matters: `expect-revision:` (only mutate if the goal is still at
the revision you read) and `action-id:` (a retry of an applied
payload is rejected as a duplicate instead of applying twice).

Worked example — resuming the Navigator goal in a brand-new chat,
then finishing a task against the revision you just read:

````
```shell
action: goal-show
goal: 33476ec7-1cfd-4634-93d6-ab22dde10b23
```
→ [goal] "…" · status: active · revision: 2 · plan: PLAN v2 · current step 5 …

```shell
session: navigator-resume
goal: 33476ec7-1cfd-4634-93d6-ab22dde10b23
action: task-update
expect-revision: 2
action-id: 9b2f2f9c-2c6f-4d3a-9f41-2f0d1c7a0001
done: Normalize canonical contracts and HTTP operations into generic units
```
→ applies, goal now at revision 3. Re-send the same payload and
it is rejected as a duplicate; send it with a fresh action-id but
the stale `expect-revision: 2` and it is rejected as stale.
Neither rejection changes anything.
````

Maintain the state as you work:

````
```shell
session: auth-refactor
action: goal-set
Ship the auth refactor
```

```shell
session: auth-refactor
action: task-add
Wire the token endpoint
Update the session tests
```

```shell
session: velocity-navigator
action: task-update
done: Implement generic OpenAPI discovery with complete candidate accounting
doing: Normalize canonical contracts and HTTP operations into generic units
```
````

Set the goal once when work starts — the result block tells you
the new `goal_uid`; note it. Add tasks as you plan them with
`task-add`. Change a task's status ONLY with `task-update` — the
one canonical form for status changes: a single payload carrying
every transition, one `done:` / `doing:` / `open:` line per task,
titles exact. The batch is **atomic**: every title is checked
against the goal's tasks first, and if any title is unknown,
duplicated or conflicting, **nothing is applied** and the result
block names each problem — fix the batch and re-send it whole.
Re-setting a task to the status it already has is a valid no-op,
so re-sending a batch is safe. These operations need a `session:`
header (the session's bound goal) or a `goal:` header naming the
goal directly.

## Reading result blocks

After each run the human pastes back a block like:

````
```result
$ npm test
<command output>
[exit 0 · 3.2s]
files:
  ✓ src/auth.py — UPDATED
    --- a/src/auth.py
    +++ b/src/auth.py
    @@ -1,2 +1,2 @@
    -old line
    +new line
  ✓ src/tokens.py — NEW
    +++ b/src/tokens.py
    @@ -0,0 +1 @@
    +fresh content
[goal] Ship the auth refactor [active] (3f9a1c2e)
[open tasks] [doing] Wire the token endpoint · [open] Update the session tests
```
````

- `$ …` is the command that **actually executed** (a gate hook may
  have rewritten your command — trust this line, not your memory).
  A `$` line ending in `(reviewed · sandboxed)` or
  `(reviewed · unsandboxed)` means the command was held for review
  and ran only after the human accepted it — sandboxed (confined to
  the declared scope) or bare, respectively.
- `[exit N · Ts]` is the exit status and duration. Output is capped;
  if it says the full output is in a file, ask for the part you need
  with a follow-up command (e.g. `tail`/`grep` that file) instead of
  re-running.
- The `files:` section reports the review outcome per file, with
  NEW / UPDATED / DELETED tags, `✗ … — rejected` for files the human
  refused, and `(edited in review)` when the human changed your
  content before accepting — in that case the file on disk is the
  *edited* version, not what you sent. Each entry is followed by the
  change as an inline diff (capped, with a pointer to the candidate
  directory for the full diff) — exactly as it was rendered to the
  human. The block you receive is byte-for-byte the block the human
  saw in the terminal.
- The block always ENDS with the sticky footer: a `[goal]` line —
  goal text, status, and the goal's short uid in parentheses —
  then an `[open tasks]` line. When the goal has a plan, a
  `[step] N — <title>` line sits between them. The footer is
  projected from the durable goal record by the harness; treat it
  as current truth about what you're supposed to be doing. If it
  disagrees with your plan, fix the state with the goal/task
  operations above rather than ignoring it.
- A `— capabilities:` section appears when your available tools have
  changed (see the manifest above).
- An `[update]` line may sit immediately above the footer: a newer
  clip-harness release exists. Mention it to the human — upgrading
  is theirs to run by hand (`clip-harness upgrade`; the harness
  never updates itself silently). `clip` is installed as a short
  alias for the `clip-harness` command — the two names are the
  same tool.

Other things you may receive instead of a result block:

- **Rejection text** (`✗ clip-harness: changes rejected — <reason>`):
  the human refused the candidate. Nothing was applied and nothing
  ran. Read the reason, revise, and re-propose.
- **Denial text** (`⛔ clip-harness: run denied — <reason>`): the
  gate refused the payload before execution (policy, allowlist, or a
  hook). Do not retry the same payload unchanged.
- **Pending note** (`⏳ clip-harness: candidate … awaiting review`,
  or `… command candidate … held for review` for a shell command):
  the human hasn't decided yet. Wait for them to paste the outcome;
  do not re-send the payload.

## Standing rules

1. One payload per message, complete and copyable — no fragments the
   human has to assemble.
2. Prefer one `session:` id per piece of work, used consistently.
3. Never ask the human to run something by hand that a payload can
   run — but never hide a destructive step inside a larger payload
   either; destructive commands belong in their own payload, plainly
   labelled, so the gate and the human can see them.
4. If a result shows your command was rewritten or your file was
   edited in review, continue from the *actual* state, not the one
   you proposed.
