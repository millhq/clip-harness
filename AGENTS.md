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

## Your capabilities (the manifest)

These are the tools you may invoke, and the block shape for each:

- **shell.run** — run shell commands.
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
  Recognised keys: `session`, `action`, `cwd`, `timeout`. Everything
  after the header is shell text, run with the user's shell in `cwd`
  (default: wherever the harness was triggered).
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
  bytes on disk in an editor; partial content would corrupt the file.
- `op:` is optional (`create` / `update` / `delete`). Declare it
  honestly when you know it: the harness classifies each file against
  disk (NEW / UPDATE / DELETE / NO-CHANGE) and flags mismatches
  between your declared op and reality.
- For a delete, use a ```file fence with `op: delete` and no content.
- Use ```patch when a small diff is clearer than a whole file; the
  diff must apply cleanly to the current file.
- File changes are **never applied directly**. They are staged as a
  candidate; the human sees a manifest (path · op · line counts) and
  a diff, then accepts, accepts with edits, or rejects. If a payload
  also has commands, the commands run only after the files are
  accepted.

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
  ✓ src/tokens.py — NEW
— goal: Ship the auth refactor [active]
  open tasks: [doing] Wire the token endpoint · [open] Update the session tests
```
````

- `$ …` is the command that **actually executed** (a gate hook may
  have rewritten your command — trust this line, not your memory).
- `[exit N · Ts]` is the exit status and duration. Output is capped;
  if it says the full output is in a file, ask for the part you need
  with a follow-up command (e.g. `tail`/`grep` that file) instead of
  re-running.
- The `files:` section reports the review outcome per file, with
  NEW / UPDATED / DELETED tags, `✗ … — rejected` for files the human
  refused, and `(edited in review)` when the human changed your
  content before accepting — in that case the file on disk is the
  *edited* version, not what you sent.
- The `— goal:` / `open tasks:` section is standing context generated
  by the harness. Treat it as current truth about what you're
  supposed to be doing; if it disagrees with your plan, fix the state
  with the goal/task operations above rather than ignoring it.
- A `— capabilities:` section appears when your available tools have
  changed (see the manifest above).

Other things you may receive instead of a result block:

- **Rejection text** (`✗ clip-harness: changes rejected — <reason>`):
  the human refused the candidate. Nothing was applied and nothing
  ran. Read the reason, revise, and re-propose.
- **Denial text** (`⛔ clip-harness: run denied — <reason>`): the
  gate refused the payload before execution (policy, allowlist, or a
  hook). Do not retry the same payload unchanged.
- **Pending note** (`⏳ clip-harness: candidate … awaiting review`):
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
