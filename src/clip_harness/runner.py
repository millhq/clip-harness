"""The lifecycle runner — the harness's state machine.

One pass through process_text():

    parse ─► PayloadCaptured (receipt FIRST) ─► PreRun (gate)
        ├─ deny / hard block / gate-hook failure ─► denial path
        │     (receipt + rejection text on clipboard, NO post hooks)
        ├─ ask or file payload ─► review branch (§7.3)
        │     stage candidate ─► manifest ─► decision data
        │     ├─ no decision ─► PENDING (candidate intact)
        │     ├─ reject ─► denial path (no post hooks)
        │     └─ accept ─► apply (verify + rollback) ─► shell, if any
        ├─ ask verdict on a shell command ─► command review branch
        │     stage a kind-"command" candidate ─► decision data
        │     ├─ no decision ─► PENDING (command candidate intact)
        │     ├─ reject ─► denial path (no post hooks)
        │     └─ accept ─► execute (sandboxed, or bare only on an
        │           explicit unsandboxed accept)
        └─ allow ─► execute ─► PostRun / PostRunFailure (separate
              events; receipt BEFORE clipboard overwrite)
              ─► RunClosed (refuses a clean close while pending)

Notifications are NOT a core feature: the core emits lifecycle events
and a notification is a hook placed at whichever event you want one
(PostRun, PostRunFailure, ReviewDecision-pending, SessionEnd...).
clip-harness ships default notify hooks for that; see `clip-harness
init`. The review DECISION, by contrast, is transport — part of
executing the flow — so the decider is an adapter, not a hook.

The runner depends only on adapter interfaces (clipboard, decider,
diff opener, executor) — never on macOS or iTerm2 directly.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import gate as gate_mod
from . import hooks as hooks_mod
from . import sandbox as sandbox_mod
from .core import (MAX_PAYLOAD_CHARS, Decision, Event, GateVerdict, Payload,
                   RunContext, RunState, cap_output, elide_output,
                   format_result, new_run_id, now_iso, sha256_text)
from .executor import Executor, LocalExecutor
from .goals import GoalStore
from .manifest import ManifestStore
from .payload import PayloadError, parse_clipboard_text
from .receipts import Ledger, Transcript
from .sessions import RunHandle, SessionRegistry
from .settings import Settings, add_project_allowlist_pattern, load_settings

ACTION_OPS = {"goal-set", "goal-status", "task-add", "task-update"}

# Single-task status actions, removed outright (no backward compat —
# migrate once to the latest): `task-update` replaces them. A payload
# naming one gets a static migration error, never a shell run of its
# body.
REMOVED_TASK_ACTIONS = {"task-doing", "task-done", "task-open"}


@dataclass
class RunOutcome:
    run_id: str
    state: RunState
    message: str
    result_block: str | None = None
    candidate_id: str | None = None


class LifecycleRunner:
    def __init__(self, *, settings: Settings, clipboard=None, decider=None,
                 command_decider=None,
                 diff_opener=None, inline_review: bool = False,
                 stream_output: bool = False,
                 stream_sink: Callable[[str], None] | None = None,
                 heartbeat_interval: float | None = None,
                 presenter: Callable[[str], None] = print,
                 executor: Executor | None = None,
                 session_id: str | None = None,
                 transcript_path: Path | None = None):
        self.settings = settings
        self.state_dir = Path(settings.state_dir).expanduser()
        self.clipboard = clipboard
        self.decider = decider
        # The decision channel for held shell COMMANDS (command
        # candidates). Falls back to `decider` when not wired
        # separately — most channels (file-drop, scripted) serve both
        # candidate kinds; the terminal channel dispatches per kind
        # (see cli.py).
        self.command_decider = command_decider
        # Stored for embedders; the runner itself never opens diffs —
        # the terminal decider owns the VS Code option (see cli.py).
        self.diff_opener = diff_opener
        # Entry-point wiring: True when a human fired this run
        # directly (the trigger / `run` paths in cli.py), so a staged
        # candidate is decided inline in the same invocation. The
        # watch entries leave it False — watched runs always park.
        self.inline_review = inline_review
        # Same wiring shape for live run visibility: True only on the
        # interactive entries at a real terminal (cli.py gates on the
        # TTY), so `_execute` presents a run header and streams the
        # command's output to the terminal as it is produced. Pure
        # presentation — the captured output, spill file, receipts
        # and result block are untouched; the watch and headless
        # entries leave it False and run byte-identically to before.
        self.stream_output = stream_output
        self.stream_sink = stream_sink
        self.heartbeat_interval = heartbeat_interval
        self.presenter = presenter
        self.executor = executor or LocalExecutor()
        self.session_id = session_id or f"sess-{time.strftime('%Y%m%d')}"
        self.ledger = Ledger(self.state_dir / "receipts.jsonl")
        self.registry = SessionRegistry(self.state_dir)
        self.manifest = ManifestStore(self.state_dir)
        self.transcript = (Transcript(transcript_path)
                           if transcript_path else None)
        self._armed = False

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def arm(self, cwd: str | Path) -> None:
        """SessionStart — fast, non-blocking, observational."""
        if self._armed:
            return
        self._armed = True
        cwd = str(cwd)
        for sub in ("candidates", "runs", "sessions", "goals"):
            (self.state_dir / sub).mkdir(parents=True, exist_ok=True)
        tools = {name: bool(shutil.which(name)) for name in
                 ("pbpaste", "pbcopy", "code", "git", "osascript", "sh")}
        self.manifest.register(self.settings.tools)

        # ConfigChange: settings edited while armed are their own events.
        hash_path = self.state_dir / "config.hash"
        current = self.settings.config_hash()
        previous = (hash_path.read_text(encoding="utf-8").strip()
                    if hash_path.exists() else None)
        if previous and previous != current:
            self.ledger.append("session", Event.CONFIG_CHANGE.value,
                               {"status": "changed", "from": previous,
                                "to": current},
                               session_id=self.session_id)
            self._dispatch_simple(Event.CONFIG_CHANGE, cwd)
        hash_path.write_text(current, encoding="utf-8")

        self.ledger.append("session", Event.SESSION_START.value,
                           {"status": "armed", "tools": tools, "cwd": cwd},
                           session_id=self.session_id)
        self._dispatch_simple(Event.SESSION_START, cwd)

    def session_end(self, cwd: str | Path) -> dict:
        summary = self.ledger.summary()
        self.ledger.append("session", Event.SESSION_END.value,
                           {"status": "closed", "summary": summary},
                           session_id=self.session_id)
        self._dispatch_simple(Event.SESSION_END, str(cwd))
        return summary

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------

    def process_text(self, text: str, cwd: str | Path,
                     allow_bare: bool = False) -> RunOutcome | None:
        """Handle one clipboard text. None = not a harness payload.

        allow_bare is set only by the explicit trigger path (the human
        hit the hotkey): bare clipboard text then runs as one unbound
        command, hotkey-run style. The passive watch loop leaves it
        False so arbitrary clipboard content never auto-executes.
        """
        cwd = str(cwd)
        self.arm(cwd)
        try:
            payload = parse_clipboard_text(text, allow_bare=allow_bare)
        except PayloadError as exc:
            return self._malformed(text, str(exc))
        if payload is None:
            return None
        if payload.kind in ("cancel", "rerun"):
            return self._handle_trigger(payload, cwd)

        run_id = new_run_id()
        ctx = RunContext(run_id=run_id, payload=payload, cwd=cwd,
                         session_id=self.session_id)
        ctx.clipboard_sha256 = sha256_text(text)
        handle = RunHandle(
            run_id=run_id, command=payload.command,
            cwd=payload.cwd or cwd, binding_id=payload.session_id,
            output_file=str(self.state_dir / "runs" / run_id / "output.txt"))
        self.registry.register(handle)
        run_dir = self.state_dir / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "payload.txt").write_text(text, encoding="utf-8")

        # --- PayloadCaptured: the receipt lands BEFORE anything else.
        if len(text) > MAX_PAYLOAD_CHARS:
            reason = (f"payload too large "
                      f"({len(text)} > {MAX_PAYLOAD_CHARS} chars)")
            self.ledger.append(run_id, Event.PAYLOAD_CAPTURED.value,
                               {"status": "rejected", "reason": reason,
                                "sha256": ctx.clipboard_sha256},
                               session_id=self.session_id,
                               binding_id=payload.session_id)
            return self._deny(ctx, handle, reason, Event.PAYLOAD_CAPTURED)
        self.ledger.append(run_id, Event.PAYLOAD_CAPTURED.value,
                           {"status": "captured",
                            "sha256": ctx.clipboard_sha256,
                            "bytes": len(text),
                            "files": len(payload.files),
                            "bound": payload.bound,
                            # The header's free-form action label,
                            # echoed into the receipt as documented.
                            "action": payload.action},
                           session_id=self.session_id,
                           binding_id=payload.session_id)
        outcome = self._dispatch(Event.PAYLOAD_CAPTURED, ctx,
                                 tool_name="Bash",
                                 tool_input={"command": payload.command},
                                 tool_use_id=run_id)
        if outcome is not None and outcome.gate_failed(Event.PAYLOAD_CAPTURED):
            return self._deny(
                ctx, handle,
                f"PayloadCaptured hook: "
                f"{outcome.hard_block_reason or outcome.error}",
                Event.PAYLOAD_CAPTURED)

        effective_cwd = (str(Path(payload.cwd).expanduser())
                         if payload.cwd else cwd)
        timeout = payload.timeout or self.settings.timeout

        # --- Goal/task operations are core actions, handled in-process.
        if payload.action in REMOVED_TASK_ACTIONS:
            return self._deny(
                ctx, handle,
                f"action {payload.action!r} was removed — task status "
                f"changes now go through one atomic 'task-update' "
                f"payload: header `action: task-update`, then one "
                f"'<status>: <exact task title>' line per transition "
                f"(done: / doing: / open:)",
                Event.PRE_RUN)
        if payload.action in ACTION_OPS:
            return self._action_op(ctx, handle, payload, effective_cwd)

        # --- PreRun: built-in policy, then hooks (see _prerun).
        verdict = self._prerun(ctx, payload)
        if verdict.hard_block:
            return self._deny(ctx, handle,
                              f"hard block: {verdict.reason}",
                              Event.PRE_RUN)
        if verdict.hook_error:
            return self._deny(ctx, handle,
                              f"gate hook failed closed: {verdict.hook_error}",
                              Event.PRE_RUN)
        if verdict.decision == Decision.DENY:
            return self._deny(ctx, handle, verdict.reason, Event.PRE_RUN)
        command = (verdict.updated_command
                   if verdict.updated_command is not None
                   else payload.command)
        ctx.command_executed = command

        files_section = ""
        if payload.files:
            result = self._mutation_branch(ctx, handle, payload,
                                           effective_cwd, timeout)
            if isinstance(result, RunOutcome):
                return result
            files_section = result
        if verdict.decision == Decision.ASK and command.strip():
            # A shell command the gate held (a mutation under
            # mutation_gate: review, or not on the allowlist) — with
            # or without file fences. It cannot be diffed, but it IS
            # reviewable: the command review branch stages it as a
            # command candidate and a decision executes it
            # (sandboxed by default) or parks it for one.
            return self._shell_review_branch(
                ctx, handle, payload, verdict, command, effective_cwd,
                timeout, files_section)

        if not command.strip():
            return self._complete_files_only(ctx, handle, files_section)
        return self._execute(ctx, handle, command, effective_cwd, timeout,
                             files_section)

    # ------------------------------------------------------------------
    # PreRun
    # ------------------------------------------------------------------

    def _prerun(self, ctx: RunContext, payload: Payload) -> GateVerdict:
        """Built-in policy first (hard blocks before allowlist), then
        hook dispatch; an updatedInput rewrite is re-checked against
        the policy before it may run."""
        command = payload.command
        if not command.strip():
            return GateVerdict()
        verdict = gate_mod.evaluate_policy(command, self.settings)
        if verdict.hard_block:
            return verdict
        outcome = self._dispatch(Event.PRE_RUN, ctx, tool_name="Bash",
                                 tool_input={"command": command},
                                 tool_use_id=ctx.run_id)
        if outcome is None:
            return verdict
        if outcome.hard_block:
            verdict.decision = Decision.DENY
            verdict.hard_block = True
            verdict.reason = outcome.hard_block_reason
            return verdict
        if outcome.error:
            verdict.hook_error = outcome.error
            return verdict
        if outcome.decision:
            rank = {Decision.ALLOW: 0, Decision.ASK: 1, Decision.DENY: 2}
            hook_decision = Decision(outcome.decision)
            if rank[hook_decision] > rank[verdict.decision]:
                verdict.decision = hook_decision
                verdict.reason = outcome.reason or verdict.reason
            if hook_decision == Decision.DENY:
                verdict.reason = outcome.reason or verdict.reason
                return verdict
        if outcome.updated_input is not None:
            new_command = outcome.updated_input.get("command")
            if not new_command:
                verdict.decision = Decision.DENY
                verdict.reason = ("hook updatedInput replaced the input "
                                  "without a command")
                return verdict
            recheck = gate_mod.evaluate_policy(new_command, self.settings)
            if recheck.hard_block or recheck.decision == Decision.DENY:
                verdict.decision = Decision.DENY
                verdict.hard_block = recheck.hard_block
                verdict.reason = (f"rewritten command blocked: "
                                  f"{recheck.reason}")
                return verdict
            verdict.updated_command = new_command
            if recheck.decision == Decision.ASK \
                    and verdict.decision == Decision.ALLOW:
                verdict.decision = Decision.ASK
                verdict.reason = recheck.reason
        return verdict

    # ------------------------------------------------------------------
    # Mutation branch (§7.3)
    # ------------------------------------------------------------------

    def _mutation_branch(self, ctx: RunContext, handle: RunHandle,
                         payload: Payload, cwd: str,
                         timeout: float) -> RunOutcome | str:
        """Returns a finished RunOutcome, or the files-section string
        when the payload may proceed to its shell part."""
        run_id = ctx.run_id
        for index, spec in enumerate(payload.files):
            target = Path(cwd) / spec.path
            if spec.is_edit:
                # Claude Code's Edit hook shape, exactly — it takes
                # precedence over the content/patch reclassifications
                # below, whose "Edit" carries a different input.
                tool_name = "Edit"
                tool_input = {"file_path": spec.path,
                              "old_string": spec.old_string,
                              "new_string": spec.new_string,
                              "replace_all": spec.replace_all}
            elif spec.declared_op == "delete":
                tool_name, tool_input = "Delete", {"file_path": spec.path}
            elif target.is_file():
                tool_name = "Edit"
                tool_input = ({"file_path": spec.path,
                               "content": spec.content}
                              if not spec.is_patch else
                              {"file_path": spec.path, "patch": spec.patch})
            else:
                tool_name = "Write"
                tool_input = {"file_path": spec.path, "content": spec.content}
            outcome = self._dispatch(Event.PRE_RUN, ctx,
                                     tool_name=tool_name,
                                     tool_input=tool_input,
                                     tool_use_id=f"{run_id}-file-{index}")
            if outcome is not None and (
                    outcome.hard_block
                    or outcome.error
                    or outcome.decision == Decision.DENY.value):
                return self._deny(ctx, handle,
                                  f"file gate ({spec.path}): "
                                  f"{outcome.hard_block_reason or outcome.error or outcome.reason}",
                                  Event.PRE_RUN)

        candidate_dir = self.state_dir / "candidates" / run_id
        try:
            candidate = gate_mod.classify_and_stage(
                candidate_dir, run_id, payload.session_id, payload.files,
                Path(cwd))
        except gate_mod.GateError as exc:
            return self._fail(ctx, handle,
                              f"candidate staging failed: {exc}",
                              extra={"stage": "candidate"})
        ctx.candidate_id = run_id
        # Manifest extras let an out-of-band review finish the whole run.
        manifest_path = candidate_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update({"shell_command": payload.command, "cwd": cwd,
                         "timeout": timeout,
                         "clipboard_sha256": ctx.clipboard_sha256})
        manifest_path.write_text(json.dumps(manifest, indent=2),
                                 encoding="utf-8")

        self.ledger.append(run_id, Event.REVIEW_DECISION.value,
                           {"status": "staged", "candidate_id": run_id,
                            "summary": candidate.summary()},
                           session_id=self.session_id,
                           binding_id=payload.session_id)
        self._dispatch_review(ctx, "staged", run_id, None)
        # Decision points: the candidate is staged and the run PARKS
        # in pending_review unless a decision is taken inline, right
        # here. That happens when the payload demands it
        # (`attend: true`) or when the entry point wired inline
        # review (the human-fired trigger / `run` paths — see cli.py),
        # for attended payloads and pure file/patch payloads alike:
        # the review surface (manifest + inline diffs) is presented
        # in the terminal and the decision is taken there. The watch
        # entries never wire inline review, so watched runs always
        # park. A decider returning None means "decide later" — the
        # run parks below with its candidate intact.
        record = None
        if payload.attend or self.inline_review:
            self.presenter(candidate.presentation()
                           + (f"\n  shell: {payload.command}"
                              if payload.command else ""))
            if self.decider is not None:
                record = self.decider(candidate)
        if record is None:
            self.registry.set_state(handle, RunState.PENDING_REVIEW.value)
            self.ledger.append(run_id, Event.REVIEW_DECISION.value,
                               {"status": "pending", "candidate_id": run_id},
                               session_id=self.session_id,
                               binding_id=payload.session_id)
            self._dispatch_review(ctx, "pending", run_id, None)
            note = (f"⏳ clip-harness: candidate {run_id} staged and "
                    f"awaiting review.\nReview it with: "
                    f"clip-harness candidate review {run_id}")
            self._set_clipboard(note)
            return RunOutcome(run_id, RunState.PENDING_REVIEW,
                              "candidate staged, awaiting review",
                              result_block=note,
                              candidate_id=run_id)
        return self._apply_decision(ctx, handle, candidate, record)

    def _apply_decision(self, ctx: RunContext, handle: RunHandle,
                        candidate: gate_mod.Candidate,
                        record: gate_mod.DecisionRecord) -> RunOutcome | str:
        run_id = ctx.run_id
        gate_mod.write_decision(candidate, record)
        self._dispatch_review(ctx, record.decision, run_id,
                              {"decision": record.decision,
                               "reason": record.reason,
                               "accepted_files": record.accepted_files})
        if record.decision == "reject":
            # Rejection: receipt + rejection text, NO post hooks fire.
            self.ledger.append(run_id, Event.REVIEW_DECISION.value,
                               {"status": "rejected",
                                "reason": record.reason,
                                "decision": record.decision},
                               session_id=self.session_id,
                               binding_id=ctx.binding_id)
            lines = ["✗ clip-harness: changes rejected"
                     + (f" — {record.reason}" if record.reason else "")]
            for entry in candidate.entries:
                lines.append(f"  ✗ {entry.path} — rejected")
                diff = gate_mod.render_diff(candidate, entry)
                if diff:
                    lines.extend(f"    {ln}" for ln in diff.splitlines())
            text = "\n".join(lines)
            self._set_clipboard(text)
            if self.transcript:
                self.transcript.append_run(now_iso(), "(file payload)",
                                           text, "rejected", 0.0)
            self.registry.set_state(handle, RunState.REJECTED.value)
            self._close(ctx, handle)
            return RunOutcome(run_id, RunState.REJECTED,
                              f"rejected: {record.reason}".strip(),
                              result_block=text)

        report = gate_mod.apply_candidate(candidate, record)
        self.ledger.append(run_id, Event.REVIEW_DECISION.value,
                           {"status": "applied" if report.ok
                            else "apply-failed",
                            "entries": [vars(e) for e in report.entries],
                            "rolled_back": report.rolled_back},
                           session_id=self.session_id,
                           binding_id=ctx.binding_id)
        if not report.ok:
            return self._fail(
                ctx, handle,
                "apply failed"
                + (" — rolled back" if report.rolled_back else ""),
                extra={"stage": "apply", "rolled_back": report.rolled_back,
                       "entries": [vars(e) for e in report.entries]},
                files_section=report.section())
        return report.section()

    # ------------------------------------------------------------------
    # Command review branch — a held shell command is reviewable
    # ------------------------------------------------------------------

    def _shell_review_branch(self, ctx: RunContext, handle: RunHandle,
                             payload: Payload, verdict: GateVerdict,
                             command: str, cwd: str, timeout: float,
                             files_section: str) -> RunOutcome:
        """Stage an ASK-verdict shell command as a command candidate
        and take a decision when a channel exists (an attended
        payload, or an entry point wired for inline review). No
        decision parks the run with the candidate intact — the
        clipboard note names the review command. For a mixed payload
        the files are already accepted at this point (files_section
        is preserved on the candidate); the command alone waits."""
        run_id = ctx.run_id
        candidate_dir = self.state_dir / "candidates" / run_id
        try:
            candidate = gate_mod.stage_command_candidate(
                candidate_dir, run_id, payload.session_id,
                command=command, cwd=cwd, timeout=timeout,
                writes=payload.writes, reason=verdict.reason,
                clipboard_sha256=ctx.clipboard_sha256,
                files_section=files_section)
        except gate_mod.GateError as exc:
            return self._fail(ctx, handle,
                              f"candidate staging failed: {exc}",
                              extra={"stage": "candidate"})
        ctx.candidate_id = run_id
        self.ledger.append(run_id, Event.REVIEW_DECISION.value,
                           {"status": "staged", "kind": "command",
                            "candidate_id": run_id, "command": command,
                            "writes": candidate.resolved_writes,
                            "reason": verdict.reason},
                           session_id=self.session_id,
                           binding_id=payload.session_id)
        self._dispatch_review(ctx, "staged", run_id, None)
        record = None
        if payload.attend or self.inline_review:
            self.presenter(candidate.presentation(
                self.settings.shell_sandbox))
            decider = self.command_decider or self.decider
            if decider is not None:
                record = decider(candidate)
        if record is None:
            self.registry.set_state(handle, RunState.PENDING_REVIEW.value)
            self.ledger.append(run_id, Event.REVIEW_DECISION.value,
                               {"status": "pending", "kind": "command",
                                "candidate_id": run_id},
                               session_id=self.session_id,
                               binding_id=payload.session_id)
            self._dispatch_review(ctx, "pending", run_id, None)
            note = (f"⏳ clip-harness: command candidate {run_id} held "
                    f"for review — {verdict.reason}\n"
                    f"Review it with: "
                    f"clip-harness candidate review {run_id}")
            self._set_clipboard(note)
            return RunOutcome(run_id, RunState.PENDING_REVIEW,
                              f"held pending review: {verdict.reason}",
                              result_block=note, candidate_id=run_id)
        return self._apply_command_decision(ctx, handle, candidate,
                                            record, files_section)

    def _apply_command_decision(self, ctx: RunContext, handle: RunHandle,
                                candidate: gate_mod.CommandCandidate,
                                record: gate_mod.DecisionRecord,
                                files_section: str = "") -> RunOutcome:
        """Carry out one command-candidate decision: reject denies
        the run (reason back to the model, no post hooks — the file
        flow's reject shape); an accept EXECUTES the command, under
        the Seatbelt sandbox built from its declared scope unless the
        human explicitly accepted it unsandboxed. "always-allow"
        additionally persists the derived allowlist pattern to the
        project settings so this command shape is not held again."""
        run_id = ctx.run_id
        decision = record.decision
        if decision not in ("accept", "accept-unsandboxed",
                            "always-allow", "reject"):
            return self._fail(
                ctx, handle,
                f"unknown decision for a command candidate: "
                f"{decision!r} (expected accept, accept-unsandboxed, "
                f"always-allow or reject)",
                extra={"stage": "review", "kind": "command"})
        mode = {"accept": "sandboxed",
                "always-allow": "sandboxed",
                "accept-unsandboxed": "unsandboxed"}.get(decision)
        if decision == "accept-unsandboxed" \
                and self.settings.shell_sandbox == "strict":
            return self._fail(
                ctx, handle,
                "unsandboxed execution refused: shell_sandbox is set "
                "to strict — a reviewed command may only run "
                "sandboxed",
                extra={"stage": "review", "kind": "command"})
        gate_mod.write_decision(candidate, record)
        decision_data = {"decision": decision, "reason": record.reason,
                         "mode": mode, "command": candidate.command,
                         "writes": candidate.resolved_writes}
        pattern = None
        if decision == "always-allow":
            pattern = gate_mod.derive_allow_pattern(candidate.command)
            decision_data["always_pattern"] = pattern
        self._dispatch_review(ctx, decision, run_id, decision_data)
        if decision == "reject":
            # Rejection: receipt + rejection text, NO post hooks fire.
            self.ledger.append(run_id, Event.REVIEW_DECISION.value,
                               {"status": "rejected", "kind": "command",
                                "reason": record.reason,
                                "decision": decision,
                                "command": candidate.command,
                                "writes": candidate.resolved_writes,
                                "mode": None},
                               session_id=self.session_id,
                               binding_id=ctx.binding_id)
            text = ("✗ clip-harness: command rejected"
                    + (f" — {record.reason}" if record.reason else "")
                    + f"\n  ✗ $ {candidate.command} — rejected")
            self._set_clipboard(text)
            if self.transcript:
                self.transcript.append_run(now_iso(), candidate.command,
                                           text, "rejected", 0.0)
            self.registry.set_state(handle, RunState.REJECTED.value)
            self._close(ctx, handle)
            return RunOutcome(run_id, RunState.REJECTED,
                              f"rejected: {record.reason}".strip(),
                              result_block=text)
        if pattern and add_project_allowlist_pattern(candidate.cwd,
                                                      pattern):
            self._note_config_change(candidate.cwd, pattern)
        profile = None
        if mode == "sandboxed":
            profile = sandbox_mod.build_profile(
                cwd=candidate.cwd,
                writes=[Path(p) for p in candidate.resolved_writes])
        self.ledger.append(run_id, Event.REVIEW_DECISION.value,
                           {"status": "approved", "kind": "command",
                            "candidate_id": run_id, "decision": decision,
                            "mode": mode, "command": candidate.command,
                            "writes": candidate.resolved_writes,
                            "reason": record.reason,
                            **({"always_pattern": pattern}
                               if pattern else {})},
                           session_id=self.session_id,
                           binding_id=ctx.binding_id)
        note = f"reviewed · {mode}"
        return self._execute(ctx, handle, candidate.command,
                             candidate.cwd, candidate.timeout,
                             files_section, sandbox_profile=profile,
                             command_note=note)

    def _note_config_change(self, cwd: str, pattern: str) -> None:
        """An always-allow decision rewrote the project settings:
        record it exactly the way arm() records a settings edit made
        while armed — ConfigChange ledger entry + hook dispatch, and
        the stored config hash moves to the new value so the change
        is reported once, here, not again at the next arm."""
        fresh = load_settings(cwd)
        current = fresh.config_hash()
        hash_path = self.state_dir / "config.hash"
        previous = (hash_path.read_text(encoding="utf-8").strip()
                    if hash_path.exists() else None)
        if previous == current:
            return
        self.ledger.append("session", Event.CONFIG_CHANGE.value,
                           {"status": "changed", "from": previous,
                            "to": current, "reason": "always-allow",
                            "pattern": pattern},
                           session_id=self.session_id)
        self._dispatch_simple(Event.CONFIG_CHANGE, cwd)
        hash_path.write_text(current, encoding="utf-8")

    def _review_command_candidate(self, candidate_id: str,
                                  candidate: gate_mod.CommandCandidate,
                                  decider) -> RunOutcome:
        """Finish a parked command candidate out-of-band (the CLI
        review path): the decider sees the command candidate, and an
        accept executes the command through _execute — PostRun
        hooks, the bound footer and the reviewed-run note included."""
        record = decider(candidate)
        if record is None:
            return RunOutcome(candidate_id, RunState.PENDING_REVIEW,
                              "still pending — no decision captured",
                              candidate_id=candidate_id)
        payload = Payload(command=candidate.command,
                          session_id=candidate.session_id,
                          writes=candidate.writes)
        ctx = RunContext(run_id=candidate_id, payload=payload,
                         cwd=candidate.cwd, session_id=self.session_id)
        ctx.clipboard_sha256 = candidate.clipboard_sha256
        ctx.candidate_id = candidate_id
        ctx.command_executed = candidate.command
        handle = RunHandle(
            run_id=candidate_id, command=candidate.command,
            cwd=candidate.cwd, binding_id=candidate.session_id,
            output_file=str(self.state_dir / "runs" / candidate_id
                            / "output.txt"))
        self.registry.register(handle)
        return self._apply_command_decision(
            ctx, handle, candidate, record, candidate.files_section)

    def review_candidate(self, candidate_id: str,
                         decider) -> RunOutcome:
        """Finish a pending candidate out-of-band (the CLI review path)."""
        candidate_dir = self.state_dir / "candidates" / candidate_id
        candidate = gate_mod.load_candidate(candidate_dir)
        if isinstance(candidate, gate_mod.CommandCandidate):
            return self._review_command_candidate(candidate_id, candidate,
                                                  decider)
        manifest = json.loads(
            (candidate_dir / "manifest.json").read_text(encoding="utf-8"))
        record = decider(candidate)
        if record is None:
            return RunOutcome(candidate_id, RunState.PENDING_REVIEW,
                              "still pending — no decision captured",
                              candidate_id=candidate_id)
        cwd = str(manifest.get("cwd", "."))
        payload = Payload(command=manifest.get("shell_command") or "",
                          session_id=candidate.session_id)
        ctx = RunContext(run_id=candidate_id, payload=payload, cwd=cwd,
                         session_id=self.session_id)
        ctx.clipboard_sha256 = manifest.get("clipboard_sha256") or ""
        ctx.candidate_id = candidate_id
        handle = RunHandle(
            run_id=candidate_id, command=payload.command, cwd=cwd,
            binding_id=candidate.session_id,
            output_file=str(self.state_dir / "runs" / candidate_id
                            / "output.txt"))
        self.registry.register(handle)
        result = self._apply_decision(ctx, handle, candidate, record)
        if isinstance(result, RunOutcome):
            return result
        if payload.command.strip():
            return self._execute(ctx, handle, payload.command, cwd,
                                 manifest.get("timeout")
                                 or self.settings.timeout, result)
        return self._complete_files_only(ctx, handle, result)

    # ------------------------------------------------------------------
    # Goal/task action ops (core — the nag must not depend on hooks)
    # ------------------------------------------------------------------

    def _action_op(self, ctx: RunContext, handle: RunHandle,
                   payload: Payload, cwd: str) -> RunOutcome:
        run_id = ctx.run_id
        if not payload.session_id:
            return self._deny(
                ctx, handle,
                f"action {payload.action!r} needs a session: header — "
                f"goal/task state is anchored to a session",
                Event.PRE_RUN)
        pre_tool_input: dict = {"action": payload.action,
                                "body": payload.command}
        if payload.action == "task-update":
            pre_tool_input["transitions"] = payload.transitions
        outcome = self._dispatch(Event.PRE_RUN, ctx, tool_name="Session",
                                 tool_input=pre_tool_input,
                                 tool_use_id=run_id)
        if outcome is not None and outcome.gate_failed(Event.PRE_RUN):
            return self._deny(ctx, handle,
                              f"action gate: "
                              f"{outcome.hard_block_reason or outcome.error}",
                              Event.PRE_RUN)
        store = GoalStore(self.state_dir, payload.session_id)
        body = payload.command.strip()
        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        output_lines: list[str] = []
        applied: list[dict] = []
        batch_errors: list[dict] = []
        if payload.action == "goal-set":
            goal = store.set_goal(" ".join(lines))
            output_lines.append(f"goal set: {goal['title']}")
        elif payload.action == "goal-status":
            status = lines[0].lower() if lines else ""
            goal = store.set_goal_status(status) \
                if status in ("active", "paused", "done") else None
            output_lines.append(
                f"goal status: {goal['status']}" if goal
                else "no goal set, or unknown status")
        elif payload.action == "task-add":
            for line in lines:
                store.add_task(line)
                output_lines.append(f"task added: {line}")
        else:  # task-update — the ONLY way to change a task's status
            applied, batch_errors = store.apply_task_transitions(
                payload.transitions)
            if batch_errors:
                output_lines.append(
                    "task-update rejected — nothing applied:")
                for error in batch_errors:
                    output_lines.append(
                        f"  {error['problem']} task: {error['title']}")
            else:
                for entry in applied:
                    if entry["noop"]:
                        output_lines.append(
                            f"task {entry['to']}: {entry['title']} "
                            f"(already {entry['to']} — no change)")
                    else:
                        output_lines.append(
                            f"task {entry['to']}: {entry['title']} "
                            f"({entry['from']} → {entry['to']})")
        output = "\n".join(output_lines) or "(no change)"
        if payload.action == "task-update":
            summary = "; ".join(
                f"{t['status']}: {t['title']}"
                for t in payload.transitions)
            ctx.command_executed = f"(task-update) {summary}".strip()
        else:
            ctx.command_executed = f"({payload.action}) {body}".strip()
        tool_response = {"exit_desc": "0", "output": output}
        if payload.action == "task-update":
            # One receipt for the batch, carrying the full transition
            # list (and, on an atomic reject, every error).
            tool_response["transitions"] = applied
            if batch_errors:
                tool_response["errors"] = batch_errors
        outcome = self._dispatch(Event.POST_RUN, ctx, tool_name="Session",
                                 tool_input={"action": payload.action},
                                 tool_use_id=run_id,
                                 extra={"tool_response": tool_response,
                                        "error": None, "duration_ms": 0})
        self.ledger.append(run_id, Event.POST_RUN.value,
                           {"status": "ok", "tool_name": "Session",
                            "tool_input": {"action": payload.action},
                            "tool_response": tool_response,
                            "duration_ms": 0},
                           session_id=self.session_id,
                           binding_id=payload.session_id)
        block = format_result(ctx.command_executed, output, "0", 0.0, "",
                              standing_section=store.section(),
                              extra_section=self._extra_section(
                                  ctx, outcome),
                              budget=self.settings.result_budget_chars)
        self._save_result(run_id, block)
        self._set_clipboard(block)
        self.registry.set_state(handle, RunState.COMPLETED.value)
        self._close(ctx, handle)
        if payload.action == "task-update" and batch_errors:
            message = "task-update rejected — nothing applied"
        else:
            message = f"{payload.action} applied"
        return RunOutcome(run_id, RunState.COMPLETED, message,
                          result_block=block)

    # ------------------------------------------------------------------
    # Execution + post events
    # ------------------------------------------------------------------

    @staticmethod
    def _run_header(command: str, cwd: str, timeout: float,
                    binding_id: str | None) -> str:
        """The live-run header presented on the terminal just before
        a command executes — what is running, where, under which
        session and time bound. Presentation only: it goes through
        the presenter channel and never enters the captured output
        or the result block."""
        meta = f"cwd: {cwd} · timeout: {timeout:g}s"
        if binding_id:
            meta += f" · session: {binding_id}"
        return f"$ {command}\n  {meta}"

    def _execute(self, ctx: RunContext, handle: RunHandle, command: str,
                 cwd: str, timeout: float,
                 files_section: str,
                 sandbox_profile: str | None = None,
                 command_note: str = "") -> RunOutcome:
        run_id = ctx.run_id
        self.registry.set_state(handle, RunState.RUNNING.value)
        stop = threading.Event()
        if handle.binding_id:
            self.registry.clear_cancel_flag(handle.binding_id)

            def _watch_flag() -> None:
                while not stop.wait(0.2):
                    if self.registry.cancel_flag_set(handle.binding_id):
                        handle.request_cancel()
                        break
            threading.Thread(target=_watch_flag, daemon=True).start()
        run_kwargs: dict = {}
        if sandbox_profile is not None:
            # A reviewed command accepted sandboxed: the executor
            # wraps the spawn in sandbox-exec (and fails closed if
            # it cannot). Composes with streaming like any other run.
            run_kwargs["sandbox_profile"] = sandbox_profile
        if self.stream_output:
            self.presenter(self._run_header(command, cwd, timeout,
                                            handle.binding_id))
            run_kwargs["sink"] = (self.stream_sink
                                  if self.stream_sink is not None
                                  else self.presenter)
            if self.heartbeat_interval is not None:
                run_kwargs["heartbeat_interval"] = \
                    self.heartbeat_interval
        result = self.executor.run(command, cwd, timeout,
                                   handle.cancel_event(),
                                   Path(handle.output_file),
                                   **run_kwargs)
        stop.set()
        ctx.result = result
        duration_ms = int(result.elapsed * 1000)
        tool_response = {"exit_desc": result.exit_desc,
                         "exit_code": result.exit_code,
                         "output": result.output[:100_000],
                         "output_path": handle.output_file}
        if result.cancelled:
            event, status = Event.POST_RUN, "cancelled"
        elif result.failed:
            event, status = Event.POST_RUN_FAILURE, "failed"
        else:
            event, status = Event.POST_RUN, "ok"
        outcome = self._dispatch(
            event, ctx, tool_name="Bash",
            tool_input={"command": command}, tool_use_id=run_id,
            extra={"tool_response": tool_response,
                   "error": result.exit_desc
                   if event is Event.POST_RUN_FAILURE else None,
                   "duration_ms": duration_ms})
        display_output = result.output
        rewritten = False
        if (outcome is not None and outcome.updated_tool_output is not None
                and event is Event.POST_RUN):
            display_output = outcome.updated_tool_output
            rewritten = True

        elision = elide_output(display_output,
                               self.settings.result_budget_chars,
                               handle.output_file)
        # Receipt BEFORE the clipboard is overwritten (§6 ordering rule).
        self.ledger.append(run_id, event.value,
                           {"status": status, "tool_name": "Bash",
                            "tool_input": {"command": command},
                            "tool_response": tool_response,
                            "duration_ms": duration_ms,
                            "output_chars": len(display_output),
                            "output_elided": elision.elided,
                            **({"output_rewritten_by_hook": True}
                               if rewritten else {})},
                           session_id=self.session_id,
                           binding_id=ctx.binding_id)
        block = format_result(command, display_output, result.exit_desc,
                              result.elapsed, handle.output_file,
                              files_section=files_section,
                              standing_section=self._standing(ctx),
                              extra_section=self._extra_section(
                                  ctx, outcome),
                              command_note=command_note,
                              budget=self.settings.result_budget_chars)
        self._save_result(run_id, block)
        if self.transcript:
            capped, _ = cap_output(display_output, handle.output_file)
            self.transcript.append_run(now_iso(), command, capped,
                                       result.exit_desc, result.elapsed)
        self._set_clipboard(block)
        if event is Event.POST_RUN_FAILURE:
            state = RunState.FAILED
        elif result.cancelled:
            state = RunState.CANCELLED
        else:
            state = RunState.COMPLETED
        self.registry.set_state(handle, state.value)
        self._close(ctx, handle)
        return RunOutcome(run_id, state,
                          f"{result.exit_desc} in {result.elapsed:.1f}s",
                          result_block=block)

    def _complete_files_only(self, ctx: RunContext, handle: RunHandle,
                             files_section: str) -> RunOutcome:
        run_id = ctx.run_id
        tool_response = {"exit_desc": "0", "files_applied": True}
        outcome = self._dispatch(Event.POST_RUN, ctx, tool_name="Write",
                                 tool_input={}, tool_use_id=run_id,
                                 extra={"tool_response": tool_response,
                                        "error": None, "duration_ms": 0})
        self.ledger.append(run_id, Event.POST_RUN.value,
                           {"status": "ok", "tool_name": "Write",
                            "tool_response": tool_response,
                            "duration_ms": 0},
                           session_id=self.session_id,
                           binding_id=ctx.binding_id)
        block = format_result("(file changes — no shell command)", "",
                              "0", 0.0, "",
                              files_section=files_section,
                              standing_section=self._standing(ctx),
                              extra_section=self._extra_section(
                                  ctx, outcome),
                              budget=self.settings.result_budget_chars)
        self._save_result(run_id, block)
        self._set_clipboard(block)
        self.registry.set_state(handle, RunState.COMPLETED.value)
        self._close(ctx, handle)
        return RunOutcome(run_id, RunState.COMPLETED, "files applied",
                          result_block=block)

    # ------------------------------------------------------------------
    # Deny / fail / malformed / triggers / close
    # ------------------------------------------------------------------

    def _deny(self, ctx: RunContext, handle: RunHandle, reason: str,
              stage: Event) -> RunOutcome:
        self.ledger.append(ctx.run_id, stage.value,
                           {"status": "denied", "reason": reason},
                           session_id=self.session_id,
                           binding_id=ctx.binding_id)
        text = f"⛔ clip-harness: run denied — {reason}"
        self._set_clipboard(text)
        if self.transcript:
            self.transcript.append_run(now_iso(),
                                       ctx.payload.command or "(payload)",
                                       text, "denied", 0.0)
        self.registry.set_state(handle, RunState.DENIED.value)
        self._close(ctx, handle)
        # Single-artifact rule: the text on the clipboard IS the
        # outcome's block — the terminal shows the same bytes.
        return RunOutcome(ctx.run_id, RunState.DENIED, f"denied: {reason}",
                          result_block=text)

    def _fail(self, ctx: RunContext, handle: RunHandle, reason: str,
              extra: dict | None = None,
              files_section: str = "") -> RunOutcome:
        outcome = self._dispatch(Event.POST_RUN_FAILURE, ctx,
                                 tool_name="Bash",
                                 tool_input={"command": ctx.command_executed},
                                 tool_use_id=ctx.run_id,
                                 extra={"error": reason, "duration_ms": 0})
        self.ledger.append(ctx.run_id, Event.POST_RUN_FAILURE.value,
                           {"status": "failed", "error": reason,
                            **(extra or {})},
                           session_id=self.session_id,
                           binding_id=ctx.binding_id)
        block = format_result(ctx.command_executed or "(gate)",
                              f"FAILED: {reason}", "error", 0.0, "",
                              files_section=files_section,
                              standing_section=self._standing(ctx),
                              extra_section=self._extra_section(
                                  ctx, outcome),
                              budget=self.settings.result_budget_chars)
        self._save_result(ctx.run_id, block)
        self._set_clipboard(block)
        self.registry.set_state(handle, RunState.FAILED.value)
        self._close(ctx, handle)
        return RunOutcome(ctx.run_id, RunState.FAILED, reason,
                          result_block=block)

    def _malformed(self, text: str, error: str) -> RunOutcome:
        run_id = new_run_id()
        self.ledger.append(run_id, Event.PAYLOAD_CAPTURED.value,
                           {"status": "rejected", "error": error,
                            "sha256": sha256_text(text)},
                           session_id=self.session_id)
        note = f"⛔ clip-harness: malformed payload — {error}"
        self._set_clipboard(note)
        return RunOutcome(run_id, RunState.DENIED,
                          f"malformed payload: {error}",
                          result_block=note)

    def _handle_trigger(self, payload: Payload, cwd: str) -> RunOutcome:
        if payload.kind == "cancel":
            ok = self.registry.request_cancel(payload.session_id)
            msg = (f"cancel requested for {payload.session_id or 'last run'}"
                   if ok else "no running run to cancel")
            self.ledger.append("trigger", Event.PAYLOAD_CAPTURED.value,
                               {"status": "trigger", "action": "cancel",
                                "ok": ok},
                               session_id=self.session_id,
                               binding_id=payload.session_id)
            return RunOutcome("trigger", RunState.CANCELLED, msg)
        handle = (self.registry.get(payload.session_id)
                  if payload.session_id else self.registry.last())
        raw = None
        if handle is not None:
            stored = (self.state_dir / "runs" / handle.run_id
                      / "payload.txt")
            if stored.exists():
                raw = stored.read_text(encoding="utf-8")
        if raw is None:
            return RunOutcome("trigger", RunState.DENIED,
                              "nothing to rerun")
        self.ledger.append("trigger", Event.PAYLOAD_CAPTURED.value,
                           {"status": "trigger", "action": "rerun",
                            "of": handle.run_id},
                           session_id=self.session_id,
                           binding_id=payload.session_id)
        outcome = self.process_text(raw, cwd, allow_bare=True)
        return outcome or RunOutcome("trigger", RunState.DENIED,
                                     "rerun payload no longer parses")

    def _close(self, ctx: RunContext, handle: RunHandle) -> bool:
        """RunClosed — refuses a clean close while a candidate pends."""
        candidate_dir = self.state_dir / "candidates" / ctx.run_id
        if (candidate_dir / "manifest.json").exists() \
                and gate_mod.read_decision(candidate_dir) is None:
            self.ledger.append(ctx.run_id, Event.RUN_CLOSED.value,
                               {"status": "deferred",
                                "reason": "candidate pending decision",
                                "candidate_id": ctx.run_id},
                               session_id=self.session_id,
                               binding_id=ctx.binding_id)
            self._dispatch(Event.RUN_CLOSED, ctx, tool_name="Bash",
                           tool_input={}, tool_use_id=ctx.run_id)
            return False
        outcome = self._dispatch(Event.RUN_CLOSED, ctx, tool_name="Bash",
                                 tool_input={}, tool_use_id=ctx.run_id)
        if outcome is not None and outcome.gate_failed(Event.RUN_CLOSED):
            self.ledger.append(
                ctx.run_id, Event.RUN_CLOSED.value,
                {"status": "deferred",
                 "reason": f"close blocked by hook: "
                           f"{outcome.hard_block_reason or outcome.error}"},
                session_id=self.session_id, binding_id=ctx.binding_id)
            return False
        self.ledger.append(ctx.run_id, Event.RUN_CLOSED.value,
                           {"status": "closed"},
                           session_id=self.session_id,
                           binding_id=ctx.binding_id)
        self.registry.set_state(handle, RunState.CLOSED.value)
        return True

    def close_run(self, run_id: str) -> bool:
        """Attempt to close a run by id (CLI finish path)."""
        for handle in self.registry.list_bound():
            if handle.run_id == run_id:
                ctx = RunContext(run_id=run_id,
                                 payload=Payload(command=handle.command,
                                                 session_id=handle.binding_id),
                                 cwd=handle.cwd,
                                 session_id=self.session_id)
                return self._close(ctx, handle)
        # Unbound runs are not persisted in the registry, but a run
        # with a staged candidate must still record its (deferred)
        # close rather than vanish — rebuild a handle from the
        # candidate manifest, which carries cwd/session/shell data.
        manifest_path = (self.state_dir / "candidates" / run_id
                         / "manifest.json")
        if manifest_path.exists():
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8"))
            handle = RunHandle(
                run_id=run_id,
                command=manifest.get("shell_command", ""),
                cwd=manifest.get("cwd", ""),
                binding_id=manifest.get("session_id"))
            ctx = RunContext(run_id=run_id,
                             payload=Payload(command=handle.command,
                                             session_id=handle.binding_id),
                             cwd=handle.cwd,
                             session_id=self.session_id)
            return self._close(ctx, handle)
        return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _standing(self, ctx: RunContext) -> str:
        """The core goal/task nag — bound runs only, hooks or no hooks."""
        if not ctx.binding_id:
            return ""
        return GoalStore(self.state_dir, ctx.binding_id).section()

    def _manifest_section(self, ctx: RunContext) -> str:
        """Refreshed capability manifest — bound runs, once per change."""
        if not ctx.binding_id:
            return ""
        return self.manifest.pending_section(ctx.binding_id,
                                             self.settings.tools)

    def _extra_section(self, ctx: RunContext, outcome=None) -> str:
        """The variable middle of a result block — everything that is
        neither the file outcome nor the sticky footer: hook-supplied
        context first, then a refreshed capability manifest. The
        footer (standing section) is appended after this, always
        last."""
        parts: list[str] = []
        if outcome is not None and outcome.additional_context:
            lines = ["— hook context:"]
            for text in outcome.additional_context:
                lines.extend(f"  {ln}" for ln in text.splitlines())
            parts.append("\n".join(lines))
        manifest = self._manifest_section(ctx)
        if manifest:
            parts.append(manifest)
        return "\n".join(parts)

    def _dispatch(self, event: Event, ctx: RunContext, *,
                  tool_name: str, tool_input: dict, tool_use_id: str,
                  extra: dict | None = None):
        if not self.settings.hook_handlers(event.value):
            return None
        payload = hooks_mod.build_hook_input(
            event=event, session_id=ctx.binding_id or ctx.session_id,
            run_id=ctx.run_id, cwd=ctx.cwd,
            clipboard_sha256=ctx.clipboard_sha256, tool_name=tool_name,
            tool_input=tool_input, tool_use_id=tool_use_id, extra=extra)
        outcome = hooks_mod.dispatch(event, payload, self.settings)
        if outcome.registered_tools:
            self.manifest.register(outcome.registered_tools)
        return outcome

    def _dispatch_review(self, ctx: RunContext, review_status: str,
                         candidate_id: str | None,
                         decision: dict | None) -> None:
        """ReviewDecision hooks observe the review (this is where a
        review-pending / rejected NOTIFICATION hook lives). They never
        override the human's explicit decision data."""
        self._dispatch(Event.REVIEW_DECISION, ctx, tool_name="Review",
                       tool_input={"candidate_id": candidate_id},
                       tool_use_id=ctx.run_id,
                       extra={"review_status": review_status,
                              "candidate_id": candidate_id,
                              "decision": decision})

    def _dispatch_simple(self, event: Event, cwd: str) -> None:
        if not self.settings.hook_handlers(event.value):
            return
        payload = hooks_mod.build_hook_input(
            event=event, session_id=self.session_id, run_id="session",
            cwd=cwd, clipboard_sha256="", tool_name="Session",
            tool_input={}, tool_use_id="session")
        outcome = hooks_mod.dispatch(event, payload, self.settings)
        if outcome.registered_tools:
            self.manifest.register(outcome.registered_tools)

    def _set_clipboard(self, text: str) -> None:
        if self.clipboard is not None:
            self.clipboard.set_text(text)

    def _save_result(self, run_id: str, block: str) -> None:
        path = self.state_dir / "runs" / run_id / "result.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(block, encoding="utf-8")

    def result_for(self, run_id: str) -> str | None:
        path = self.state_dir / "runs" / run_id / "result.txt"
        return path.read_text(encoding="utf-8") if path.exists() else None

    def pending_candidates(self) -> list:
        """All undecided candidates (file and command kinds), oldest
        first — the `candidate list` surface."""
        root = self.state_dir / "candidates"
        found = []
        if root.exists():
            for child in sorted(root.iterdir()):
                if (child / "manifest.json").exists() and \
                        gate_mod.read_decision(child) is None:
                    found.append(gate_mod.load_candidate(child))
        return found
