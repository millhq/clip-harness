# clip-harness

A human-in-the-loop execution bridge: run AI-chat-proposed commands
from your clipboard, behind a Claude Code-style hook lifecycle with
a fail-closed review gate.

You work somewhere the chat model you can use (e.g. M365 Copilot)
has no tools, no MCP, no shell — it can only talk. But it can
*write text*, and you can copy that text. clip-harness is the other
half of that channel: the model proposes commands and file changes
as clipboard payloads, you hit one hotkey, and the harness executes
them locally — behind a review gate you control, with a receipt for
everything, and the result copied back to the clipboard for you to
paste into the chat. Durable goals survive the chat: lose the
session, the terminal, the whole conversation, and the work's
state is still there.

Why not an API or MCP? The chat surface this was built for offers
neither — the clipboard is the one channel that exists. If your
model has real tools, you don't need this. If it can only talk,
this gives it hands, with you on the gate.

```
  chat ── payload ──▶ clipboard ── hotkey ──▶ clip-harness
   ▲                                              │ review gate
   │                                              ▼ (you decide)
   └────── result block ◀──── execution: shell + files
```

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

Elsewhere: [GitHub](https://github.com/millhq/clip-harness) ·
[PyPI](https://pypi.org/project/clip-harness/) ·
[Releases](https://github.com/millhq/clip-harness/releases)
