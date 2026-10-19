# Sample session (headless)

`run_demo.py` plays both sides of the clipboard on any machine —
no Mac, no iTerm2, no GUI:

- the "Copilot" writes payloads into a file-backed clipboard;
- the harness runs them through the full lifecycle (gate, review,
  receipts, goal/task nag, capability manifest);
- a "reviewer" thread plays the human by dropping `decision.json`
  files into candidate directories — the exact same file and schema
  the production review flow reads;
- the shipped default notify hook posts the notifications (to a log
  file off macOS).

It runs four payloads — a bound command, an accepted mutation, a
rejected mutation, and a bare unbound command — asserts the
outcomes, and prints every result block plus the full
`receipts.jsonl` at the end.

    python3 examples/sample_session/run_demo.py
