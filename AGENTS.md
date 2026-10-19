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
- **patch.apply** — propose a change to one file as a unified diff.
  Staged and reviewed like file.write.
  Shape: one ```patch fence per file.
- **session.cancel** — cancel the running run for a session.
  Shape: a single line `` ```cancel``` ``, optionally `id=<session>`.
- **session.rerun** — re-run a session's last payload.
  Shape: a single line `` ```rerun``` ``, optionally `id=<session>`.
- **goal.set** — set the session's active goal.
  Shape: a ```shell fence with header `action: goal-set`.
- **task.add** — add open tasks to the session.
  Shape: a ```shell fence with header `action: task-add`, one task
  per body line.
- **task.update** — move a task's status.
  Shape: a ```shell fence with header `action: task-doing` /
  `task-done` / `task-open`; body = the exact task title.

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
  `writes`. Everything after the header is shell text, run with the
  user's shell in `cwd` (default: wherever the harness was
  triggered).
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

## Emitting file changes: ```file and ```patch fences

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
- Use ```patch when a small diff is clearer than a whole file; the
  diff must apply cleanly to the current file.
- File changes are **never applied directly**. They are staged as a
  candidate; the human sees a manifest (path · op · line counts) and
  an inline diff per file, then accepts, accepts with edits, or
  rejects (VS Code is the deep-review option for the same candidate).
  By default the trigger does **not** wait for that decision: the run
  parks in `pending_review`, the human is notified, and apply happens
  when the decision arrives. Only with `attend: true` does the
  trigger wait inline for the decision. If a payload also has
  commands, the commands run only after the files are accepted.

## Goal and task operations

Goal/task state is how you avoid drifting: the harness shows the
active goal and open tasks back to you in **every** bound result
block. Maintain it:

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
session: auth-refactor
action: task-done
Wire the token endpoint
```
````

Set the goal once when work starts; add tasks as you plan them; mark
them `task-doing` / `task-done` as they progress. These operations
require a `session:` header — they are anchored to the session.

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
[goal] Ship the auth refactor [active]
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
- The block always ENDS with the sticky footer: a `[goal]` line then
  an `[open tasks]` line, standing context generated by the harness.
  Treat it as current truth about what you're supposed to be doing;
  if it disagrees with your plan, fix the state with the goal/task
  operations above rather than ignoring it.
- A `— capabilities:` section appears when your available tools have
  changed (see the manifest above).

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
