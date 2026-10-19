"""clip-harness command line.

The DEFAULT entry (no arguments) is the static trigger path: the
chain is the user's own Apple Shortcut/hotkey (e.g. Cmd+Shift+E)
opening iTerm2 plus an iTerm2
hotkey firing a fixed sequence that runs this CLI — so zero arguments
must do the whole job: read clipboard, parse, run, result back to
clipboard. Everything variable comes from the block itself. On this
path bare clipboard text also runs (allow_bare): the human explicitly
hit the trigger, exactly like the old hotkey-run tool.

Subcommands exist for everything else: watch, run, the hook
dispatcher, the review gate (candidate/apply), sessions, the core
goal/task store, receipts, and init (install the default hooks).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import __version__
from .contract import validate_hook_input
from .core import Event
from .gate import CommandCandidate, DecisionRecord, load_candidate, \
    read_decision, write_decision
from .goals import GoalStore
from .hooks import dispatch
from .payload import Payload
from .receipts import Ledger
from .render import paint, printer
from .runner import LifecycleRunner
from .sessions import SessionRegistry
from .settings import load_settings

DEFAULT_SESSION = os.environ.get("CLIP_HARNESS_SESSION", "default")


# ----------------------------------------------------------------------
# Construction helpers
# ----------------------------------------------------------------------

def _default_clipboard():
    clip_file = os.environ.get("CLIP_HARNESS_CLIPBOARD_FILE")
    if clip_file:
        from .adapters.headless import FileClipboard
        return FileClipboard(clip_file)
    if shutil.which("pbpaste"):
        from .adapters.macos import PbClipboard
        return PbClipboard()
    return None


def _default_decider(settings=None, diff_opener=None):
    """The inline decision channel, consulted when a staged candidate
    is decided in the same invocation (the trigger / `run` paths, or
    an `attend: true` payload). At a terminal the decider IS the
    review surface, with VS Code offered inside its prompt — the
    opener is handed to the decider, never fired by the caller.
    Headless, the file-drop decider may poll generously: drop a
    decision.json into the candidate directory to decide the run.
    The wait is bounded by CLIP_HARNESS_ATTEND_WAIT_S (default
    600s); no decision inside the window means the run parks.

    One channel serves both candidate kinds: file candidates get the
    file prompt, command candidates the command prompt (sandboxed /
    unsandboxed / always-allow / reject); the file-drop decider is
    kind-agnostic — it just reads the candidate's decision.json."""
    from .adapters.headless import (FileDropDecider, TerminalCommandDecider,
                                    TerminalDecider)
    if sys.stdin.isatty():
        out = printer(settings.color if settings else "auto")
        terminal = TerminalDecider(out=out, diff_opener=diff_opener)
        command_terminal = TerminalCommandDecider(
            out=out,
            shell_sandbox=(settings.shell_sandbox if settings
                           else "optional"))

        def decide(candidate):
            if isinstance(candidate, CommandCandidate):
                return command_terminal(candidate)
            return terminal(candidate)

        return decide
    wait = float(os.environ.get("CLIP_HARNESS_ATTEND_WAIT_S", "600"))
    return FileDropDecider(timeout_s=wait)


def _default_diff_opener():
    if shutil.which("code"):
        from .adapters.macos import VSCodeDiffOpener
        return VSCodeDiffOpener()
    return None


def _runner(settings, **overrides) -> LifecycleRunner:
    opener = _default_diff_opener()
    kwargs = dict(settings=settings, clipboard=_default_clipboard(),
                  decider=_default_decider(settings, diff_opener=opener),
                  diff_opener=opener,
                  presenter=printer(settings.color))
    kwargs.update(overrides)
    return LifecycleRunner(**kwargs)


def _stream_kwargs() -> dict:
    """Live run visibility for the interactive entries: enabled only
    when a human is watching a real terminal (stdout is a TTY). The
    sink writes output chunks verbatim — no paint, since rendering
    applies to finished blocks — while the run header still goes
    through the presenter. The watch entries never call this: they
    are passive and stay byte-identical to before."""
    if not sys.stdout.isatty():
        return {}

    def _sink(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    return {"stream_output": True, "stream_sink": _sink}


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------

def cmd_trigger(args) -> int:
    """Zero-argument path: clipboard -> run -> clipboard."""
    settings = load_settings()
    clipboard = _default_clipboard()
    if clipboard is None:
        print("clip-harness: no clipboard available (need pbpaste, or "
              "set CLIP_HARNESS_CLIPBOARD_FILE)", file=sys.stderr)
        return 1
    text = clipboard.get_text()
    if not text:
        return 0
    # The human fired the trigger: a staged candidate is reviewed
    # inline, in this invocation (deferring parks it as usual), and
    # a running command is visible live on the terminal.
    runner = _runner(settings, clipboard=clipboard, inline_review=True,
                     **_stream_kwargs())
    outcome = runner.process_text(text, os.getcwd(), allow_bare=True)
    if outcome is None:
        return 0
    if outcome.result_block:
        print(paint(outcome.result_block, settings.color))
    else:
        print(f"clip-harness: {outcome.state.value} — {outcome.message}")
    return 0


def cmd_watch(args) -> int:
    settings = load_settings()
    if args.backend == "iterm2":
        from .adapters.iterm2_watch import run_iterm2_watch
        run_iterm2_watch(settings, settings.state_dir, os.getcwd())
        return 0
    from .adapters.watch_local import WatchLoop
    clipboard = _default_clipboard()
    if clipboard is None:
        print("clip-harness: no clipboard available", file=sys.stderr)
        return 1
    # The watch is passive: it never prompts inline and never opens
    # an editor — staged candidates park for a later review.
    runner = _runner(settings, clipboard=clipboard,
                     decider=None, diff_opener=None)
    WatchLoop(runner, clipboard, os.getcwd()).run_forever()
    return 0


def cmd_run(args) -> int:
    settings = load_settings()
    if args.stdin:
        text = sys.stdin.read()
    elif args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    elif args.command is not None:
        if args.session:
            text = (f"```shell\nsession: {args.session}\n"
                    f"{args.command}\n```")
        else:
            text = args.command
    else:  # clipboard
        clipboard = _default_clipboard()
        if clipboard is None:
            print("clip-harness: no clipboard available", file=sys.stderr)
            return 1
        text = clipboard.get_text() or ""
    runner = _runner(settings, inline_review=True, **_stream_kwargs())
    outcome = runner.process_text(text, os.getcwd(), allow_bare=True)
    if outcome is None:
        print("clip-harness: no payload found", file=sys.stderr)
        return 1
    if outcome.result_block:
        print(paint(outcome.result_block, settings.color))
    else:
        print(f"clip-harness: {outcome.state.value} — {outcome.message}")
    return 0


def cmd_hook(args) -> int:
    """The single dispatcher entry point: one JSON object in, at most
    one JSON object out, diagnostics to stderr."""
    try:
        event = Event(args.event)
    except ValueError:
        print(f"clip-harness hook: unknown event {args.event!r}",
              file=sys.stderr)
        return 1
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"clip-harness hook: unparseable stdin JSON: {exc}",
              file=sys.stderr)
        return 1
    errors = validate_hook_input(payload)
    if errors:
        print("clip-harness hook: input schema violation: "
              + "; ".join(errors), file=sys.stderr)
        return 1
    settings = load_settings()
    outcome = dispatch(event, payload, settings)
    specific: dict = {}
    if outcome.decision:
        specific = {"hookEventName": event.value,
                    "permissionDecision": outcome.decision,
                    "permissionDecisionReason": outcome.reason}
    if outcome.updated_input is not None:
        specific.setdefault("hookEventName", event.value)
        specific["updatedInput"] = outcome.updated_input
    if outcome.updated_tool_output is not None:
        specific.setdefault("hookEventName", event.value)
        specific["updatedToolOutput"] = outcome.updated_tool_output
    if outcome.additional_context:
        specific.setdefault("hookEventName", event.value)
        specific["additionalContext"] = "\n".join(outcome.additional_context)
    if outcome.registered_tools:
        specific.setdefault("hookEventName", event.value)
        specific["registeredTools"] = outcome.registered_tools
    if specific:
        print(json.dumps({"hookSpecificOutput": specific}))
    if outcome.hard_block:
        print(f"clip-harness hook: hard block: "
              f"{outcome.hard_block_reason}", file=sys.stderr)
        return 2
    if outcome.error:
        print(f"clip-harness hook: {outcome.error}", file=sys.stderr)
        from .core import GATE_EVENTS
        if event in GATE_EVENTS:
            return 2  # gate hooks fail closed — surfaced as a block
    return 0


def cmd_init(args) -> int:
    from .defaults import write_defaults
    config_dir = Path(args.config_dir or "~/.config/clip-harness")
    written = write_defaults(config_dir)
    for path in written:
        print(f"wrote {path}")
    print("Default hooks installed (notifications are hooks — edit or "
          "remove them in the hooks settings any time).")
    return 0


def cmd_goal(args) -> int:
    settings = load_settings()
    store = GoalStore(settings.state_dir, args.session)
    if args.goal_cmd == "set":
        goal = store.set_goal(args.title)
        print(f"goal set [{args.session}]: {goal['title']}")
    elif args.goal_cmd == "show":
        goal = store.goal()
        print(f"goal [{args.session}]: "
              f"{goal['title']} [{goal['status']}]" if goal
              else f"goal [{args.session}]: (none set)")
        for task in store.tasks():
            print(f"  [{task['status']}] {task['title']}")
    elif args.goal_cmd == "status":
        goal = store.set_goal_status(args.status)
        print(f"goal status [{args.session}]: {goal['status']}"
              if goal else "no goal set")
    elif args.goal_cmd == "task-add":
        task = store.add_task(args.title)
        print(f"task added [{args.session}]: {task['title']}")
    elif args.goal_cmd == "task-update":
        task = store.set_task_status(args.title, args.status)
        print(f"task [{args.session}]: {task['title']} -> {task['status']}"
              if task else f"task not found: {args.title}")
        return 0 if task else 1
    elif args.goal_cmd == "task-list":
        for task in store.tasks():
            print(f"[{task['status']}] {task['title']}")
    return 0


def _static_decider(record: DecisionRecord):
    return lambda candidate: record


def cmd_candidate(args) -> int:
    settings = load_settings()
    runner = _runner(settings)
    if args.candidate_cmd == "list":
        pending = runner.pending_candidates()
        if not pending:
            print("no pending candidates")
        for candidate in pending:
            print(candidate.summary())
        return 0
    candidate_dir = settings.state_dir / "candidates" / args.id
    if not (candidate_dir / "manifest.json").exists():
        print(f"clip-harness: no candidate {args.id!r}", file=sys.stderr)
        return 1
    candidate = load_candidate(candidate_dir)
    is_command = isinstance(candidate, CommandCandidate)
    if args.candidate_cmd == "show":
        if is_command:
            print(paint(candidate.presentation(settings.shell_sandbox),
                        settings.color))
        else:
            print(paint(candidate.presentation(), settings.color))
        return 0
    decision_arg = getattr(args, "decision", None)
    if decision_arg and is_command and decision_arg == "accept-edited":
        print("clip-harness: accept-edited does not apply to a command "
              "candidate — use accept, accept-unsandboxed or reject",
              file=sys.stderr)
        return 1
    if decision_arg and not is_command \
            and decision_arg == "accept-unsandboxed":
        print("clip-harness: accept-unsandboxed applies only to "
              "command candidates — use accept, accept-edited or reject",
              file=sys.stderr)
        return 1
    if args.candidate_cmd == "decide":
        if not args.decision:
            print("clip-harness: decide needs --decision "
                  "accept|accept-edited|accept-unsandboxed|reject",
                  file=sys.stderr)
            return 1
        record = DecisionRecord(
            decision=args.decision, reason=args.reason or "",
            accepted_files=(args.files.split(",") if args.files else None))
        write_decision(candidate, record)
        print(f"decision recorded for {args.id}: {record.decision}"
              " (run `clip-harness candidate apply "
              f"{args.id}` to complete the run)")
        return 0
    if args.candidate_cmd == "apply":
        record = read_decision(candidate_dir)
        if record is None:
            print("clip-harness: no decision.json for this candidate — "
                  "review it first (`candidate review` or "
                  "`candidate decide`)", file=sys.stderr)
            return 1
        outcome = runner.review_candidate(
            args.id, _static_decider(record))
    else:  # review
        if args.decision:
            record = DecisionRecord(
                decision=args.decision, reason=args.reason or "",
                accepted_files=(args.files.split(",")
                                if args.files else None))
            decider = _static_decider(record)
        elif sys.stdin.isatty():
            # Terminal review: VS Code is an option inside the file
            # prompt (owned by the decider), never opened upfront;
            # command candidates get the command prompt.
            if is_command:
                from .adapters.headless import TerminalCommandDecider
                decider = TerminalCommandDecider(
                    out=printer(settings.color),
                    shell_sandbox=settings.shell_sandbox)
            else:
                from .adapters.headless import TerminalDecider
                decider = TerminalDecider(out=printer(settings.color),
                                          diff_opener=runner.diff_opener)
        else:
            print("clip-harness: review needs a decision — pass "
                  "--decision accept|accept-edited|accept-unsandboxed|"
                  "reject (or run in a terminal)", file=sys.stderr)
            return 1
        outcome = runner.review_candidate(args.id, decider)
    if outcome.result_block:
        print(paint(outcome.result_block, settings.color))
    else:
        print(f"clip-harness: {outcome.state.value} — {outcome.message}")
    return 0


def cmd_sessions(args) -> int:
    settings = load_settings()
    registry = SessionRegistry(settings.state_dir)
    handles = registry.list_bound()
    if not handles:
        print("no bound sessions")
        return 0
    for handle in handles:
        print(f"{handle.binding_id}  run={handle.run_id}  "
              f"state={handle.state}  cwd={handle.cwd}  "
              f"$ {handle.command[:60]}")
    return 0


def cmd_cancel(args) -> int:
    settings = load_settings()
    registry = SessionRegistry(settings.state_dir)
    ok = registry.request_cancel(args.id)
    print(f"cancel {'requested' if ok else 'not found'}"
          f" for {args.id or 'last run'}")
    return 0 if ok else 1


def cmd_rerun(args) -> int:
    settings = load_settings()
    runner = _runner(settings)
    outcome = runner._handle_trigger(
        Payload(kind="rerun", session_id=args.id), os.getcwd())
    if outcome.result_block:
        print(paint(outcome.result_block, settings.color))
    else:
        print(f"clip-harness: {outcome.state.value} — {outcome.message}")
    return 0


def cmd_finish(args) -> int:
    settings = load_settings()
    runner = _runner(settings)
    run_id = args.run
    if run_id is None and args.session:
        handle = runner.registry.get(args.session)
        run_id = handle.run_id if handle else None
    if run_id is None:
        last = runner.registry.last()
        run_id = last.run_id if last else None
    if run_id is None:
        print("clip-harness: no runs recorded", file=sys.stderr)
        return 1
    block = runner.result_for(run_id)
    if block is None:
        # Still attempt the close: for a run parked in pending_review
        # this RECORDS the deferred close (RunClosed receipt) instead
        # of silently dropping the run.
        closed = runner.close_run(run_id)
        print(f"clip-harness: no finished result for run {run_id} — "
              + ("closed" if closed else
                 "close deferred: a review decision is still pending"),
              file=sys.stderr)
        return 1
    closed = runner.close_run(run_id)
    if args.copy:
        clipboard = _default_clipboard()
        if clipboard is not None:
            clipboard.set_text(block)
    print(paint(block, settings.color))
    print(f"# run {run_id} closed: {closed}", file=sys.stderr)
    return 0


def cmd_receipt(args) -> int:
    settings = load_settings()
    ledger = Ledger(settings.state_dir / "receipts.jsonl")
    if args.summary:
        print(json.dumps(ledger.summary(), indent=2))
        return 0
    entries = ledger.entries(run_id=args.run)
    if args.tail:
        entries = entries[-args.tail:]
    for entry in entries:
        print(json.dumps(entry, ensure_ascii=False))
    return 0


def cmd_version(args) -> int:
    print(f"clip-harness {__version__}")
    return 0


# ----------------------------------------------------------------------
# Parser
# ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clip-harness",
        description="Human-in-the-loop execution bridge: clipboard "
                    "payloads in, reviewed execution, receipts out.")
    sub = parser.add_subparsers(dest="cmd")

    watch = sub.add_parser("watch", help="clipboard loop daemon")
    watch.add_argument("--backend", choices=("local", "iterm2"),
                       default="local")
    watch.set_defaults(fn=cmd_watch)

    run = sub.add_parser("run", help="execute one payload")
    src = run.add_mutually_exclusive_group()
    src.add_argument("--clipboard", action="store_true")
    src.add_argument("--stdin", action="store_true")
    src.add_argument("--file")
    src.add_argument("--command")
    run.add_argument("--session",
                     help="bind a --command run to this session id")
    run.set_defaults(fn=cmd_run)

    hook = sub.add_parser("hook", help="hook dispatcher (JSON in/out)")
    hook.add_argument("--event", required=True)
    hook.set_defaults(fn=cmd_hook)

    init = sub.add_parser("init", help="install default hooks + settings")
    init.add_argument("--config-dir")
    init.set_defaults(fn=cmd_init)

    goal = sub.add_parser("goal", help="core goal/task store")
    goal.add_argument("--session", default=DEFAULT_SESSION)
    gsub = goal.add_subparsers(dest="goal_cmd", required=True)
    g = gsub.add_parser("set"); g.add_argument("title")
    gsub.add_parser("show")
    g = gsub.add_parser("status"); g.add_argument("status")
    g = gsub.add_parser("task-add"); g.add_argument("title")
    g = gsub.add_parser("task-update")
    g.add_argument("title"); g.add_argument("status")
    gsub.add_parser("task-list")
    goal.set_defaults(fn=cmd_goal)

    cand = sub.add_parser("candidate", help="review gate candidates")
    csub = cand.add_subparsers(dest="candidate_cmd", required=True)
    csub.add_parser("list")
    c = csub.add_parser("show"); c.add_argument("id")
    for name in ("review", "decide"):
        c = csub.add_parser(name)
        c.add_argument("id")
        c.add_argument("--decision",
                       choices=("accept", "accept-edited",
                                "accept-unsandboxed", "reject"))
        c.add_argument("--reason", default="")
        c.add_argument("--files",
                       help="comma-separated paths to accept "
                            "(default: all)")
    c = csub.add_parser("apply"); c.add_argument("id")
    cand.set_defaults(fn=cmd_candidate)

    sub.add_parser("sessions", help="list bound sessions") \
        .set_defaults(fn=cmd_sessions)

    cancel = sub.add_parser("cancel", help="cancel a bound/last run")
    cancel.add_argument("id", nargs="?")
    cancel.set_defaults(fn=cmd_cancel)

    rerun = sub.add_parser("rerun", help="re-run a bound/last payload")
    rerun.add_argument("id", nargs="?")
    rerun.set_defaults(fn=cmd_rerun)

    finish = sub.add_parser("finish",
                            help="print (and close) a run's result block")
    finish.add_argument("--run")
    finish.add_argument("--session")
    finish.add_argument("--copy", action="store_true",
                        help="also copy the result block to the clipboard")
    finish.set_defaults(fn=cmd_finish)

    receipt = sub.add_parser("receipt", help="read the JSONL run ledger")
    receipt.add_argument("--run")
    receipt.add_argument("--tail", type=int)
    receipt.add_argument("--summary", action="store_true")
    receipt.set_defaults(fn=cmd_receipt)

    sub.add_parser("version").set_defaults(fn=cmd_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not hasattr(args, "fn"):
        return cmd_trigger(args)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
