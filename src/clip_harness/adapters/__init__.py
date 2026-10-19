"""Tool-mapping adapters.

The core maps onto known tools through these small interfaces —
clipboard, notifications, VS Code diff review, the terminal. Two
families:

- production (macOS): pbpaste/pbcopy, osascript, `code --diff`, iTerm2.
- headless: file-backed clipboard, log notifier, decision.json-drop
  decider — so the whole loop can be exercised and QA'd anywhere,
  including CI and this project's own examples/.

Nothing in the core imports a concrete adapter.
"""
