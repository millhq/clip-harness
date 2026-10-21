# clip-harness

A human-in-the-loop execution bridge: run AI-chat-proposed commands
from your clipboard, behind a Claude Code-style hook lifecycle with
a fail-closed review gate.

```mermaid
flowchart LR
    A[chat] -->|payload| B[clipboard]
    B -->|hotkey| C[clip-harness]
    C --> D{review gate — you decide}
    D --> E[execution: shell + files]
    E -->|result block| A
```

## See it work

The model emits a payload like:

````
```shell
session: auth-refactor
npm test
```
````

…and you paste back one result block — the same bytes in your
terminal and on the clipboard, ending with the goal footer that
re-anchors both of you:

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
[goal] Ship the auth refactor [active] (3f9a1c2e)
[open tasks] [doing] wire tokens · [open] update tests
```
````

Install: `pipx install clip-harness` — then paste
[AGENTS.md](https://github.com/millhq/clip-harness/blob/main/AGENTS.md)
into your chat once to teach the model the protocol.

## Find it fast

- [The payload protocol](protocol.md) — the fences, execution
  modes, the result block, the capability manifest. The single
  source of truth for the protocol.
- [Durable goals](goals.md) — goal / plan / task, the file store,
  export & import, storage migrations.
- [Hook lifecycle & the review gate](hooks.md) — the hook
  contract, the review flow, reviewed shell commands, settings.
- [CLI & updating](cli.md) — the command surface and the verified
  self-upgrade.
- [Architecture & security](architecture.md) — design, module map,
  security model, platform notes.
- [Troubleshooting](troubleshooting.md)
- [Development](development.md)

## What it is

You work somewhere the chat model you can use (e.g. M365 Copilot)
has no tools, no MCP, no shell — it can only talk, but it can
*write text*, and you can copy that text. clip-harness is the
other half of that channel: the model proposes commands and file
changes as clipboard payloads, you hit one hotkey, and the harness
executes them locally — behind a review gate you control, with a
receipt for everything, and the result copied back to the
clipboard for you to paste into the chat. Durable goals survive
the chat: lose the session, the terminal, the whole conversation,
and the work's state is still there.

Why not an API or MCP? The chat surface this was built for offers
neither — the clipboard is the one channel that exists. If your
model has real tools, you don't need this; if it can only talk,
this gives it hands, with you on the gate.

## Goal · Plan · Task

Three separate things, on purpose:

- **Goal** — the objective: a durable anchor with a stable id.
  Sessions bind to it; it outlives them.
- **Plan** — a Markdown document the model authors whole. The
  harness stores it verbatim and never parses it.
- **Task** — the only tracked execution state (`open` / `doing` /
  `done`). What is current is read off the task states; there is
  no stored "current step" that could disagree with them.

Elsewhere: [GitHub](https://github.com/millhq/clip-harness) ·
[PyPI](https://pypi.org/project/clip-harness/) ·
[Releases](https://github.com/millhq/clip-harness/releases)
