# Durable goals (core)

Goal, plan and task are three different things here — the
separation Anthropic's Claude Code model converged on — and the
harness keeps them separate:

## Goal — the anchor

A goal is the objective that frames the work: a durable object
with a stable id, referenced by sessions, never owned by them.
Its record carries only anchor fields: `goal_uid` (harness-issued
uuid4, immutable, never derived from the session name or the goal
text), `human_id` (a readable slug), the verbatim goal text,
`status` (`waiting` / `active` / `paused` / `complete` /
`branched`), `revision` (starts at 1, +1 on every mutation),
`latest_result`, free-form `provenance`, and timestamps. A goal
holds no plan and no tasks — it anchors them. Sessions hold only
a **binding** (session id → goal_uid): lose the chat, the
terminal and the session, and the goal is still there —
`goal-list` rediscovers it, `goal-show` reconciles it, by exact
uid.

## Plan — a document

A plan is a Markdown **document**: at most one per goal, authored
whole, replaced whole. This is Anthropic's shape — in Claude
Code a "plan" is the plan-mode document, not a data structure.
The model writes it with `action: plan-set` (the payload body IS
the document) and reads it back verbatim with
`action: plan-show`; the harness stores and returns it exactly
as written and never parses steps, statuses, or structure out
of it. A plan's version identity ("PLAN v2") is the document's
own content — its title line — not schema. `goal-show` points at
the plan (exists? + title line) without embedding it.

## Task — the tracked work

A task is the only tracked execution state: its own object,
belonging to a goal — `task_id` (harness-issued, stable), the
`goal_uid`, a title, a `status` (`open` / `doing` / `done`), and
an integer `position`. There is no "current step" cursor
anywhere: which task is current is read off the task states
(the one marked `doing`), never stored separately where it
could disagree with them. Tasks are created with `task-add`
(positions assigned after the current maximum) and their
statuses change only through `task-update` — one atomic batch
of `<status>: <exact task title>` lines per payload.

## The store is files

The durable store is plain files under `<state_dir>` —
Anthropic's literal mechanism (their tasks are JSON files in a
folder; the folder is the store):

```
goals/<goal_uid>/goal.json          the goal record
goals/<goal_uid>/tasks/<id>.json    one file per task
goals/<goal_uid>/plan.md            the plan document (if any)
goals/<goal_uid>/actions.jsonl      applied action-ids (replay log)
bindings/<session_id>.json          session -> goal_uid
```

Writes go to a temp file in the same directory and land with an
atomic rename; a batch validates fully before anything is
written; each goal's mutations are serialized by a per-goal
lock, so concurrent harness processes cannot corrupt or lose
each other's updates (a lock that cannot be acquired within
~15s fails loudly, naming the lock path — it is never stolen).
Still zero third-party dependencies.

Two guards ride the mutations:

- **Compare-and-swap.** A payload carrying `expect-revision: N`
  mutates the goal only while its revision is N; a stale writer
  is rejected with both revisions named and nothing applied.
- **Idempotency.** A payload carrying `action-id: <uuid>` that
  was already applied is rejected as a duplicate before anything
  executes or mutates, with the original outcome named. Action
  ids are scoped per goal when a goal is involved, then per
  session, then global.

From the terminal:

```sh
clip-harness goal --session auth-refactor set "Ship the auth refactor"
clip-harness goal --session auth-refactor task-add "wire tokens"
clip-harness goal --session auth-refactor task-update "wire tokens" doing
clip-harness goal --session auth-refactor show          # the bound goal
clip-harness goal list                                  # every durable goal
clip-harness goal show <goal_uid>                       # one goal
clip-harness goal export <goal_uid>                     # JSON, to stdout
clip-harness goal export --all --out backup.json
clip-harness goal import backup.json
```

From the chat side, via `action:` payloads (see the
[capability manifest](protocol.md#capability-manifest)):
the first `goal-set` in a session creates the durable goal and
returns its `goal_uid` in the result block. `goal-list` /
`goal-show` are the read-only discovery pair; `plan-set` /
`plan-show` carry the plan document. Every bound result block's
sticky footer projects from the durable record (session state
never overrides it).

**Export / backup.** `goal export` writes a versioned JSON
document — `{"format": "clip-harness/goal-export@2", "goal":
{…anchor fields…}, "tasks": [{title, status}…], "plan":
"<markdown or null>"}` (or `"goals": […]` of such bundles with
`--all`). That document IS the backup format and the import
format: `goal import` validates it, preserves uid, revision,
status, provenance and timestamps exactly, recreates the tasks
as fresh task objects in position order, stores the plan
verbatim, and refuses to overwrite an existing `goal_uid` (the
refusal names it). Applied action history is NOT exported — an
import starts a fresh replay log in the new store. The retired
`@1` format is no longer imported: an `@1` document gets an
error naming the situation and pointing at `@2`. Export is
backup/restore only — day-to-day, a goal can equally be
recreated from scratch through payloads (`goal-set` +
`plan-set` + `task-add` + `task-update` is a lossless create
path now).

**One-time storage migrations.** Both run automatically on
first store open, under a store lock (two processes starting
together cannot double-import):

- A 0.2.x SQLite ledger (`<state_dir>/goals.db`) is read once
  and written out as the file layout: the plan dict is rendered
  to a Markdown document (title line from its revision label,
  one `N. [status] title` line per step — a rendering, whose
  content is user/model-owned from then on), tasks become task
  files, backlog entries become open tasks appended after them
  (the backlog field itself is deleted), and bindings and
  applied action-ids carry over. The database is renamed
  `goals.db.migrated` and never read again.
- Pre-0.2.0 per-session JSON files (`<state_dir>/goals/
  <session>.json`) migrate as they always did — fresh uid, the
  session binding recorded, legacy status `done` → `complete`,
  source renamed `.migrated` — except sessions the SQLite
  migration already bound are skipped (their file is still
  renamed, never re-read).
