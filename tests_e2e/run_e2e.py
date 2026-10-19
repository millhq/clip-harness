#!/usr/bin/env python3
"""End-to-end QA matrix for the clip-harness CLI.

Drives the REAL CLI (via _cli_shim.py -> clip_harness.cli:main) the
way a user would: payloads in through stdin / the file clipboard,
decisions through the candidate commands, assertions on real
outcomes — files on disk, clipboard bytes, receipts.jsonl events,
exit codes, wall-clock behaviour.

    python3 tests_e2e/run_e2e.py [case numbers...]

Prints one PASS/FAIL line per case; exits non-zero on any failure.
Every case gets a fresh temp HOME/project/state, so cases are
independent and the script is re-runnable.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SHIM = Path(__file__).resolve().parent / "_cli_shim.py"
PY = sys.executable


class Fail(AssertionError):
    pass


def check(cond, msg):
    if not cond:
        raise Fail(msg)


class Env:
    """One isolated playground: HOME, project (with .clip-harness
    settings), state dir, file clipboard."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="clip-e2e-"))
        self.home = self.root / "home"
        self.proj = self.root / "proj"
        self.state = self.root / "state"
        self.hooks_dir = self.root / "hooks"
        for d in (self.home, self.proj, self.state, self.hooks_dir):
            d.mkdir(parents=True)
        (self.proj / ".clip-harness").mkdir()
        self.clip = self.root / "clip.txt"
        self.clip.write_text("", encoding="utf-8")
        self._bg_n = 0
        self.write_settings({})

    # -- setup ------------------------------------------------------
    def write_settings(self, extra: dict):
        settings = {"state_dir": str(self.state)}
        settings.update(extra)
        (self.proj / ".clip-harness" / "settings.json").write_text(
            json.dumps(settings), encoding="utf-8")

    def hook(self, name: str, source: str) -> str:
        path = self.hooks_dir / name
        path.write_text(source, encoding="utf-8")
        return f"{PY} {path}"

    def write_file(self, rel: str, content: str) -> Path:
        path = self.proj / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    # -- CLI driving --------------------------------------------------
    def _env(self, over: dict | None = None) -> dict:
        # Headless posture: the file-drop decision window is zero
        # unless a case opts into waiting (case 44) — trigger / run
        # consult the decider inline, and with no decision dropped
        # the run must park promptly, not poll for the 600s default.
        env = {"HOME": str(self.home), "PATH": os.environ["PATH"],
               "CLIP_HARNESS_CLIPBOARD_FILE": str(self.clip),
               "CLIP_HARNESS_STATE": str(self.state),
               "CLIP_HARNESS_ATTEND_WAIT_S": "0"}
        if over:
            env.update(over)
        return env

    def cli(self, *args, stdin: str | None = None, timeout: float = 30,
            env_over: dict | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                [PY, str(SHIM), *args], input=stdin, text=True,
                capture_output=True, cwd=self.proj, timeout=timeout,
                env=self._env(env_over))
        except subprocess.TimeoutExpired:
            raise Fail(f"CLI {' '.join(args)} timed out after {timeout}s")

    def run_payload(self, text: str, **kw) -> subprocess.CompletedProcess:
        return self.cli("run", "--stdin", stdin=text, **kw)

    def trigger(self, **kw) -> subprocess.CompletedProcess:
        return self.cli(**kw)

    def bg(self, *args, env_over: dict | None = None):
        """Start a CLI process in the background; returns (proc,
        stdout_path). Used for long runs / attended waits."""
        self._bg_n += 1
        out_path = self.root / f"bg-{self._bg_n}.out"
        fh = open(out_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            [PY, str(SHIM), *args], stdout=fh, stderr=subprocess.STDOUT,
            cwd=self.proj, env=self._env(env_over))
        proc._out_fh = fh
        return proc, out_path

    def bg_finish(self, proc, out_path, timeout: float = 25) -> str:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise Fail("background CLI did not finish in time")
        proc._out_fh.close()
        return out_path.read_text(encoding="utf-8")

    # -- observations -------------------------------------------------
    def clip_text(self) -> str:
        return self.clip.read_text(encoding="utf-8")

    def receipts(self) -> list[dict]:
        path = self.state / "receipts.jsonl"
        if not path.exists():
            return []
        return [json.loads(ln) for ln in
                path.read_text(encoding="utf-8").splitlines() if ln]

    def events(self, event: str | None = None,
               run_id: str | None = None) -> list[dict]:
        out = self.receipts()
        if event:
            out = [e for e in out if e["event"] == event]
        if run_id:
            out = [e for e in out if e["run_id"] == run_id]
        return out

    def candidate_id(self) -> str:
        match = re.search(r"candidate (r-[0-9a-f]+)", self.clip_text())
        check(match, f"no candidate id in clipboard: {self.clip_text()!r}")
        return match.group(1)

    def manifest(self, cid: str) -> dict:
        return json.loads((self.state / "candidates" / cid
                           / "manifest.json").read_text(encoding="utf-8"))

    def session_state(self, session_id: str) -> str | None:
        path = self.state / "sessions" / f"{session_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))["state"]

    def wait_for(self, pred, timeout: float = 12, what: str = "condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return
            time.sleep(0.1)
        raise Fail(f"timed out waiting for {what}")


def artifact_check(env: Env, proc, msg: str = ""):
    """Single-artifact invariant: what the terminal printed IS the
    clipboard text, byte for byte (print adds one trailing newline)."""
    check(proc.stdout == env.clip_text() + "\n",
          f"terminal output != clipboard text {msg}: "
          f"{proc.stdout!r} vs {env.clip_text()!r}")


def footer_lines(block: str) -> list[str]:
    lines = block.rstrip().splitlines()
    if lines and lines[-1] == "```":
        lines = lines[:-1]
    return lines


# =======================================================================
# Payload forms (1-6)
# =======================================================================

def case_01(env: Env):
    proc = env.run_payload('echo "hello world"')
    check(proc.returncode == 0, f"exit {proc.returncode}: {proc.stderr}")
    block = env.clip_text()
    check(block.startswith("```result"), f"not a result block: {block!r}")
    check('$ echo "hello world"' in block, "command line missing")
    check("\nhello world\n" in block, "output missing")
    check("[exit 0" in block, "exit line missing")
    check("[goal]" not in block, "unbound run must not carry the footer")
    artifact_check(env, proc, "(case 1)")


def case_02(env: Env):
    proc = env.run_payload("```shell\necho fenced-unbound\n```")
    check(proc.returncode == 0, proc.stderr)
    check("fenced-unbound" in env.clip_text(), "output missing")
    check("[goal]" not in env.clip_text(), "unbound block has a footer")
    captured = env.events("PayloadCaptured")
    check(captured and captured[-1]["data"]["bound"] is False,
          "run should be receipted as unbound")
    check(not list((env.state / "sessions").glob("*.json")),
          "unbound run registered a session")


def case_03(env: Env):
    proc = env.run_payload(
        "```shell\nsession: sess-3\necho bound-run\n```")
    check(proc.returncode == 0, proc.stderr)
    block = env.clip_text()
    check("[goal]" in block, "bound block missing the footer")
    captured = env.events("PayloadCaptured")
    check(captured and captured[-1]["binding_id"] == "sess-3",
          f"receipt binding_id wrong: {captured}")
    run_id = captured[-1]["run_id"]
    post = env.events("PostRun", run_id=run_id)
    check(post and post[-1]["binding_id"] == "sess-3",
          "PostRun receipt not keyed by the binding")
    check((env.state / "sessions" / "sess-3.json").exists(),
          "session not registered")
    artifact_check(env, proc, "(case 3)")


def case_04(env: Env):
    env.write_settings({"timeout": 1})  # harness default: 1s
    sub = env.proj / "sub"
    sub.mkdir()
    proc = env.run_payload(
        f"```shell\nsession: sess-4\naction: deploy the thing\n"
        f"cwd: {sub}\ntimeout: 10\npwd && sleep 2\n```")
    check(proc.returncode == 0, proc.stderr)
    block = env.clip_text()
    check(str(sub) in block, f"cwd override ignored: {block!r}")
    check("[exit 0" in block,
          f"header timeout override ignored (settings default 1s): "
          f"{block!r}")
    captured = env.events("PayloadCaptured")
    check(captured and captured[-1]["data"].get("action")
          == "deploy the thing",
          f"action label not in receipt: {captured}")


def case_05(env: Env):
    proc = env.run_payload("```run\necho legacy-alias\n```")
    check(proc.returncode == 0, proc.stderr)
    check("legacy-alias" in env.clip_text(),
          "legacy ```run fence did not execute")


def case_06(env: Env):
    # Empty clipboard on the trigger path: nothing runs, no crash.
    proc = env.trigger()
    check(proc.returncode == 0, f"empty trigger exit {proc.returncode}")
    check(env.clip_text() == "", "empty clipboard was modified")
    # Whitespace via run --stdin: no payload, nothing executes.
    proc = env.run_payload("   \n  ")
    check(proc.returncode in (0, 1), f"whitespace exit {proc.returncode}")
    check(env.events("PostRun") == [], "whitespace payload executed")
    # Oversized payload (>32k): refused, nothing executed.
    env.clip.write_text("x" * 40000, encoding="utf-8")
    proc = env.trigger()
    check("payload too large" in env.clip_text(),
          f"oversized payload not refused: {env.clip_text()!r}")
    check(env.events("PostRun") == [], "oversized payload executed")
    rejected = [e for e in env.events("PayloadCaptured")
                if e["data"].get("status") == "rejected"]
    check(rejected, "oversized refusal not receipted")


# =======================================================================
# Execution control (7-10)
# =======================================================================

def case_07(env: Env):
    env.write_file("payload.txt",
                   "```shell\nsession: long-7\ntimeout: 60\nsleep 30\n```")
    proc, out_path = env.bg("run", "--file", "payload.txt")
    env.wait_for(lambda: env.session_state("long-7") == "running",
                 what="long run to start")
    env.clip.write_text("```cancel id=long-7```", encoding="utf-8")
    cancel = env.trigger()
    check(cancel.returncode == 0, cancel.stderr)
    out = env.bg_finish(proc, out_path)
    check("[exit cancelled" in out, f"run not cancelled: {out!r}")
    cancelled = [e for e in env.events("PostRun")
                 if e["data"].get("status") == "cancelled"]
    check(cancelled, "no cancelled PostRun receipt")
    check(env.session_state("long-7") != "running",
          "session still marked running after cancel")


def case_08(env: Env):
    env.run_payload(
        "```shell\nsession: rr-8\n"
        "python3 -c \"open('ticks.txt','a').write('x\\n')\"\n```")
    ticks = env.proj / "ticks.txt"
    check(ticks.read_text().count("x") == 1, "first run did not tick")
    env.clip.write_text("```rerun id=rr-8```", encoding="utf-8")
    proc = env.trigger()
    check(proc.returncode == 0, proc.stderr)
    check(ticks.read_text().count("x") == 2,
          "rerun did not re-execute the last command")
    posts = [e for e in env.events("PostRun")
             if "ticks.txt" in json.dumps(e["data"])]
    check(len(posts) == 2, f"expected 2 PostRun receipts, got {len(posts)}")


def case_09(env: Env):
    start = time.monotonic()
    proc = env.run_payload("```shell\ntimeout: 1\nsleep 10\n```",
                           timeout=20)
    elapsed = time.monotonic() - start
    check(proc.returncode == 0, proc.stderr)
    check(elapsed < 8, f"timeout not enforced (took {elapsed:.1f}s)")
    check("[exit timeout" in env.clip_text(),
          f"no timeout result: {env.clip_text()!r}")


def case_10(env: Env):
    # 12,000 lines x 20 chars = 240,000 chars of output: past the
    # default 100k result budget, so the block carries a head + tail
    # digest around the elision marker while the spill file keeps the
    # full output byte-for-byte.
    env.write_file("gen.py",
                   "for i in range(12000):\n"
                   "    print(f'row {i:05d} ' + 'x' * 9)\n")
    proc = env.run_payload("```shell\npython3 gen.py\n```", timeout=60)
    check(proc.returncode == 0, proc.stderr)
    block = env.clip_text()
    match = re.search(
        r"\[output elided: (\d+) of (\d+) chars shown · "
        r"full output \(\d+ chars\) saved at (\S+)", block)
    check(match, f"no elision marker in block: {block[:200]!r}")
    shown, total = int(match.group(1)), int(match.group(2))
    check(total == 240000, f"marker total wrong: {total}")
    check(0 < shown < 100000, f"marker shown wrong: {shown}")
    spilled = Path(match.group(3))
    check(spilled.exists() and spilled.stat().st_size == 240000,
          "full output not preserved in the spill file")
    check("row 00000" in block and "row 11999" in block,
          "digest is missing the head or the tail")
    check("row 06000" not in block, "elided middle leaked into the block")
    check(len(block) < 110000,
          "result block not bounded near the default budget")


# =======================================================================
# Mutations & review (11-15)
# =======================================================================

def _stage(env: Env, payload: str) -> str:
    proc = env.run_payload(payload)
    check(proc.returncode == 0, proc.stderr)
    check("awaiting review" in env.clip_text(),
          f"payload did not park: {env.clip_text()!r}")
    return env.candidate_id()


def case_11(env: Env):
    env.write_file("upd.txt", "old\n")
    env.write_file("del.txt", "bye\n")
    cid = _stage(env,
                 "```file\npath: new.txt\nop: create\nfresh\n```\n"
                 "```file\npath: upd.txt\nop: update\nnew\n```\n"
                 "```file\npath: del.txt\nop: delete\n```")
    entries = {e["path"]: e for e in env.manifest(cid)["entries"]}
    check(entries["new.txt"]["actual_op"] == "NEW",
          f"create misclassified: {entries['new.txt']}")
    check(entries["upd.txt"]["actual_op"] == "UPDATE",
          f"update misclassified: {entries['upd.txt']}")
    check(entries["del.txt"]["actual_op"] == "DELETE",
          f"delete misclassified: {entries['del.txt']}")


def case_12(env: Env):
    env.write_file("real.txt", "existing\n")
    cid = _stage(env,
                 "```file\npath: real.txt\nop: create\ndifferent\n```")
    entry = env.manifest(cid)["entries"][0]
    check(entry["actual_op"] == "UPDATE" and entry["mismatch"] is True,
          f"mismatch not flagged in manifest: {entry}")
    show = env.cli("candidate", "show", cid)
    check("declared create" in show.stdout,
          f"mismatch not shown in review surface: {show.stdout!r}")


def case_13(env: Env):
    target = env.write_file("same.txt", "identical\n")
    mtime = target.stat().st_mtime_ns
    cid = _stage(env, "```file\npath: same.txt\nidentical\n```")
    entry = env.manifest(cid)["entries"][0]
    check(entry["actual_op"] == "NO_CHANGE",
          f"identical file not classified NO_CHANGE: {entry}")
    proc = env.cli("candidate", "review", cid, "--decision", "accept")
    check(proc.returncode == 0, proc.stderr)
    check("· same.txt — no change" in env.clip_text(),
          f"no-change not reported: {env.clip_text()!r}")
    check(target.read_text() == "identical\n"
          and target.stat().st_mtime_ns == mtime,
          "no-change file was rewritten")


def case_14(env: Env):
    env.write_file("upd.txt", "v1\n")
    cid = _stage(env,
                 "```file\npath: made.txt\nop: create\nmade\n```\n"
                 "```file\npath: upd.txt\nop: update\nv2\n```")
    proc = env.cli("candidate", "review", cid, "--decision", "accept")
    check(proc.returncode == 0, proc.stderr)
    check((env.proj / "made.txt").read_text() == "made\n",
          "NEW file not applied")
    check((env.proj / "upd.txt").read_text() == "v2\n",
          "UPDATE file not applied")
    block = env.clip_text()
    check("✓ made.txt — NEW" in block, f"NEW tag missing: {block!r}")
    check("✓ upd.txt — UPDATED" in block,
          f"UPDATED tag missing: {block!r}")
    applied = [e for e in env.events("ReviewDecision", run_id=cid)
               if e["data"].get("status") == "applied"]
    check(applied, "no applied ReviewDecision receipt")
    statuses = {en["path"]: en["status"]
                for en in applied[-1]["data"]["entries"]}
    check(statuses == {"made.txt": "applied", "upd.txt": "applied"},
          f"receipt entries wrong: {statuses}")


def case_15(env: Env):
    env.write_file("edit.txt", "v1\n")
    cid = _stage(env, "```file\npath: edit.txt\nop: update\nv2\n```")
    staged = env.state / "candidates" / cid / "candidates" / "0"
    staged.write_text("v2\nreviewer tweak\n", encoding="utf-8")
    proc = env.cli("candidate", "review", cid, "--decision",
                   "accept-edited")
    check(proc.returncode == 0, proc.stderr)
    check((env.proj / "edit.txt").read_text() == "v2\nreviewer tweak\n",
          "edited bytes were not the applied bytes")
    check("(edited in review)" in env.clip_text(),
          "block does not flag the reviewer edit")
    applied = [e for e in env.events("ReviewDecision", run_id=cid)
               if e["data"].get("status") == "applied"]
    entries = applied[-1]["data"]["entries"]
    check(entries and entries[0]["reviewer_modified"] is True,
          f"receipt does not flag reviewer_modified: {entries}")


# =======================================================================
# Mutations & review, continued (16-22)
# =======================================================================

def case_16(env: Env):
    cid = _stage(env,
                 "```file\npath: keep.txt\nkeep\n```\n"
                 "```file\npath: drop.txt\ndrop\n```")
    proc = env.cli("candidate", "review", cid, "--decision", "accept",
                   "--files", "keep.txt")
    check(proc.returncode == 0, proc.stderr)
    check((env.proj / "keep.txt").exists(), "accepted file not applied")
    check(not (env.proj / "drop.txt").exists(),
          "non-accepted file was applied")
    block = env.clip_text()
    check("✓ keep.txt — NEW" in block, f"accept line missing: {block!r}")
    check("✗ drop.txt — rejected" in block,
          f"rejected line missing: {block!r}")
    applied = [e for e in env.events("ReviewDecision", run_id=cid)
               if e["data"].get("status") == "applied"]
    statuses = {en["path"]: en["status"]
                for en in applied[-1]["data"]["entries"]}
    check(statuses.get("drop.txt") == "rejected",
          f"partial rejection not receipted: {statuses}")


def case_17(env: Env):
    cid = _stage(env, "```file\npath: nope.txt\nnope\n```")
    proc = env.cli("candidate", "review", cid, "--decision", "reject",
                   "--reason", "wrong layer")
    check(proc.returncode == 0, proc.stderr)
    check(not (env.proj / "nope.txt").exists(), "rejected file written")
    text = env.clip_text()
    check("changes rejected — wrong layer" in text,
          f"rejection text/reason missing: {text!r}")
    check("✗ nope.txt — rejected" in text, "rejected entry missing")
    check(env.events("PostRun", run_id=cid) == [],
          "PostRun fired for a rejected run")
    check(env.events("PostRunFailure", run_id=cid) == [],
          "PostRunFailure fired for a rejected run")
    artifact_check(env, proc, "(case 17)")


def case_18(env: Env):
    cid = _stage(env, "```file\npath: pure.txt\npure\n```")
    proc = env.cli("candidate", "review", cid, "--decision", "accept")
    check(proc.returncode == 0, proc.stderr)
    check((env.proj / "pure.txt").read_text() == "pure\n",
          "pure mutation not applied")
    check("$ (file changes — no shell command)" in env.clip_text(),
          "an empty command was attempted")


def case_19(env: Env):
    env.write_file("doc.txt", "line one\nline two\nline three\n")
    patch = ("```patch\npath: doc.txt\n--- a/doc.txt\n+++ b/doc.txt\n"
             "@@ -1,3 +1,3 @@\n line one\n-line two\n+line TWO\n"
             " line three\n```")
    cid = _stage(env, patch)
    staged = env.state / "candidates" / cid / "candidates" / "0"
    check(staged.read_text() == "line one\nline TWO\nline three\n",
          f"staged candidate is not the patched content: "
          f"{staged.read_text()!r}")
    show = env.cli("candidate", "show", cid)
    check("-line two" in show.stdout and "+line TWO" in show.stdout,
          "review surface does not show the patch diff")
    proc = env.cli("candidate", "review", cid, "--decision", "accept")
    check(proc.returncode == 0, proc.stderr)
    check((env.proj / "doc.txt").read_text()
          == "line one\nline TWO\nline three\n", "patch not applied")


def case_20(env: Env):
    env.write_file("doc.txt", "actual content\n")
    patch = ("```patch\npath: doc.txt\n--- a/doc.txt\n+++ b/doc.txt\n"
             "@@ -1 +1 @@\n-not the content\n+replacement\n```")
    proc = env.run_payload(patch)
    out = proc.stdout + env.clip_text()
    check("does not apply cleanly" in out,
          f"patch failure not surfaced: {out!r}")
    check((env.proj / "doc.txt").read_text() == "actual content\n",
          "live file touched by a failed patch")
    check(env.events("PostRun") == [], "failed patch still ran")


def case_21(env: Env):
    env.write_file("a.txt", "a-original\n")
    env.write_file("b.txt", "b-original\n")
    cid = _stage(env,
                 "```file\npath: a.txt\na-updated\n```\n"
                 "```file\npath: b.txt\nb-updated\n```")
    # B moves on disk after staging -> its base-hash check fails
    # mid-apply, after A has already been written.
    (env.proj / "b.txt").write_text("b-moved\n", encoding="utf-8")
    proc = env.cli("candidate", "review", cid, "--decision", "accept")
    check(proc.returncode == 0, proc.stderr)
    check((env.proj / "a.txt").read_text() == "a-original\n",
          "earlier file was not rolled back")
    check((env.proj / "b.txt").read_text() == "b-moved\n",
          "failed file was overwritten")
    block = env.clip_text()
    check("↩ a.txt — rolled back" in block,
          f"rollback not reported in block: {block!r}")
    failed = [e for e in env.events("ReviewDecision", run_id=cid)
              if e["data"].get("status") == "apply-failed"]
    check(failed and failed[-1]["data"].get("rolled_back") is True,
          f"rollback not receipted: {failed}")


def case_22(env: Env):
    cid = _stage(env, "```file\npath: pending.txt\npending\n```")
    proc = env.cli("finish", "--run", cid)
    check(proc.returncode == 1,
          f"finish on a pending run exited {proc.returncode}")
    check("deferred" in proc.stderr,
          f"deferred close not reported: {proc.stderr!r}")
    deferred = [e for e in env.events("RunClosed", run_id=cid)
                if e["data"].get("status") == "deferred"]
    check(deferred, "deferred close not recorded in receipts")
    listing = env.cli("candidate", "list")
    check(cid in listing.stdout, "pending candidate was silently dropped")


# =======================================================================
# Gate & hooks (23-30)
# =======================================================================

DENY_HOOK = """
import json, sys
json.load(sys.stdin)
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreRun", "permissionDecision": "deny",
    "permissionDecisionReason": "not today, policy"}}))
"""

SIDE_EFFECT = ("python3 -c \"open('side.txt','a').write('x')\"")


def case_23(env: Env):
    env.write_settings({"hooks": {
        "PreRun": [{"command": env.hook("deny.py", DENY_HOOK)}]}})
    proc = env.run_payload(f"```shell\n{SIDE_EFFECT}\n```")
    check(proc.returncode == 0, proc.stderr)
    check(not (env.proj / "side.txt").exists(), "denied command executed")
    check("run denied" in env.clip_text()
          and "not today, policy" in env.clip_text(),
          f"deny reason not on clipboard: {env.clip_text()!r}")
    check("not today, policy" in
          (env.state / "receipts.jsonl").read_text(encoding="utf-8"),
          "deny reason not in receipts")


def case_24(env: Env):
    allow = env.hook("allow.py", """
import json, sys
json.load(sys.stdin)
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreRun", "permissionDecision": "allow"}}))
""")
    block = env.hook("block.py", """
import sys
sys.stderr.write("hard stop here")
sys.exit(2)
""")
    env.write_settings({
        "allowlist": ["python3 *"],
        "hooks": {"PreRun": [{"command": allow}, {"command": block}]}})
    proc = env.run_payload(f"```shell\n{SIDE_EFFECT}\n```")
    check(proc.returncode == 0, proc.stderr)
    check(not (env.proj / "side.txt").exists(),
          "exit-2 hook did not hard-block")
    check("hard stop here" in env.clip_text(),
          f"hard-block reason missing: {env.clip_text()!r}")


def case_25(env: Env):
    rewrite = env.hook("rewrite.py", """
import json, sys
data = json.load(sys.stdin)
cmd = (data.get("tool_input") or {}).get("command", "")
new = None
if "ORIGINAL" in cmd:
    new = {"command": "echo REWRITTEN-OK"}
elif "MAKEBAD" in cmd:
    new = {"command": "echo forbidden-thing"}
if new:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreRun", "updatedInput": new}}))
""")
    env.write_settings({
        "denylist": ["*forbidden*"],
        "hooks": {"PreRun": [{"command": rewrite}]}})
    proc = env.run_payload("```shell\necho ORIGINAL\n```")
    check(proc.returncode == 0, proc.stderr)
    block = env.clip_text()
    check("$ echo REWRITTEN-OK" in block and "REWRITTEN-OK" in block,
          f"rewritten command did not execute: {block!r}")
    proc = env.run_payload("```shell\necho MAKEBAD\n```")
    check("rewritten command blocked" in env.clip_text(),
          f"rewrite was not re-checked against policy: "
          f"{env.clip_text()!r}")
    check(env.events("PostRun") != []
          and len(env.events("PostRun")) == 1,
          "the denied rewrite still executed")


def _gate_fail_case(env: Env, hook_cmd: str, label: str):
    env.write_settings({"hooks": {"PreRun": [{"command": hook_cmd}]}})
    proc = env.run_payload(f"```shell\n{SIDE_EFFECT}\n```", timeout=20)
    check(proc.returncode == 0, proc.stderr)
    check(not (env.proj / "side.txt").exists(),
          f"{label}: command executed despite the broken gate hook")
    check("⛔" in env.clip_text() or "denied" in env.clip_text(),
          f"{label}: run not blocked: {env.clip_text()!r}")


def case_26(env: Env):
    crash = env.hook("crash.py", "import sys\nsys.exit(1)\n")
    _gate_fail_case(env, crash, "crash")
    badjson = env.hook("badjson.py", "print('this is not json')\n")
    _gate_fail_case(env, badjson, "bad JSON")
    slow = env.hook("slow.py", "import time\ntime.sleep(5)\n")
    env.write_settings({"hooks": {
        "PreRun": [{"command": slow, "timeout": 1}]}})
    start = time.monotonic()
    proc = env.run_payload(f"```shell\n{SIDE_EFFECT}\n```", timeout=20)
    elapsed = time.monotonic() - start
    check(proc.returncode == 0, proc.stderr)
    check(not (env.proj / "side.txt").exists(),
          "timeout: command executed despite the timed-out gate hook")
    check(elapsed < 8, f"hook timeout not enforced ({elapsed:.1f}s)")
    reasons = (env.state / "receipts.jsonl").read_text(encoding="utf-8")
    check("gate hook" in reasons or "hook" in env.clip_text(),
          "fail-closed reason not recorded")


def case_27(env: Env):
    crash = env.hook("postcrash.py", "import sys\nsys.exit(1)\n")
    env.write_settings({"hooks": {"PostRun": [{"command": crash}]}})
    proc = env.run_payload("```shell\necho still-fine\n```")
    check(proc.returncode == 0, proc.stderr)
    check("[exit 0" in env.clip_text() and "still-fine" in env.clip_text(),
          f"PostRun hook crash affected the result: {env.clip_text()!r}")
    artifact_check(env, proc, "(case 27)")


def case_28(env: Env):
    recorder = env.hook("recorder.py", """
import json, os, sys
data = json.load(sys.stdin)
with open(os.environ["E2E_EVENT_LOG"], "a") as fh:
    fh.write(data.get("hook_event_name", "?") + "\\n")
""")
    env.write_settings({"hooks": {
        "PostRun": [{"command": recorder}],
        "PostRunFailure": [{"command": recorder}]}})
    log = env.root / "events.log"
    proc = env.run_payload("```shell\nls /nonexistent-xyz-123\n```",
                           env_over={"E2E_EVENT_LOG": str(log)})
    check(proc.returncode == 0, proc.stderr)
    check("[exit 0" not in env.clip_text(), "failing command reported 0")
    seen = log.read_text().splitlines() if log.exists() else []
    check(seen == ["PostRunFailure"],
          f"expected only PostRunFailure to fire, got {seen}")
    runs = {e["run_id"] for e in env.events("PayloadCaptured")}
    check(any(env.events("PostRunFailure", run_id=r) for r in runs),
          "no PostRunFailure receipt")
    check(all(env.events("PostRun", run_id=r) == [] for r in runs),
          "PostRun receipt exists for a failed run")


def case_29(env: Env):
    # Ordering: the PostRun receipt must exist in receipts.jsonl
    # BEFORE the clipboard is overwritten. Witness: the clipboard is
    # a FIFO — the harness's write unblocks only when the reader
    # opens it, and the reader snapshots the ledger at that instant.
    fifo = env.root / "clip.fifo"
    os.mkfifo(fifo)
    env.clip = fifo  # Env.cli points CLIP_HARNESS_CLIPBOARD_FILE here
    sink: dict = {}

    def reader():
        with open(fifo, "r", encoding="utf-8") as fh:
            ledger = env.state / "receipts.jsonl"
            sink["ledger_at_write"] = (
                ledger.read_text(encoding="utf-8")
                if ledger.exists() else "")
            sink["text"] = fh.read()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    proc = env.run_payload("```shell\nsession: ord-29\necho orderly\n```",
                           timeout=25)
    thread.join(timeout=10)
    check("text" in sink, "clipboard write never happened")
    check(proc.returncode == 0, proc.stderr)
    check('"event": "PostRun"' in sink["ledger_at_write"],
          "PostRun receipt was not in the ledger before the "
          "clipboard was overwritten")
    check(sink["text"] + "\n" == proc.stdout,
          "what was written to the clipboard differs from what the "
          "terminal printed (single-artifact invariant)")
    check("orderly" in sink["text"], "result block missing from FIFO")


def case_30(env: Env):
    # (a) No hooks configured -> notifications simply do not exist:
    # notifications are hooks, nothing else posts them.
    env.write_settings({"hooks": {}})
    env.run_payload("```shell\necho quiet\n```")
    env.run_payload("```shell\nls /nonexistent-xyz-123\n```")
    _stage(env, "```file\npath: q.txt\nq\n```")
    check(not (env.state / "notifications.log").exists(),
          "a notification appeared with no hooks configured")
    check(not (env.home / ".local" / "state" / "clip-harness"
               / "notifications.log").exists(),
          "a notification appeared in the default state dir")
    # (b) The shipped defaults (init) -> done / failure /
    # review-pending notifications fire.
    env2 = Env()
    init = env2.cli("init")
    check(init.returncode == 0, init.stderr)
    check((env2.home / ".config" / "clip-harness"
           / "settings.json").exists(), "init did not install settings")
    env2.run_payload("```shell\necho loud\n```")
    env2.run_payload("```shell\nls /nonexistent-xyz-123\n```")
    _stage(env2, "```file\npath: p.txt\np\n```")
    notes = (env2.state / "notifications.log").read_text(encoding="utf-8")
    check("clip-harness: done" in notes,
          f"done notification missing: {notes!r}")
    check("clip-harness: run FAILED" in notes,
          f"failure notification missing: {notes!r}")
    check("clip-harness: review pending" in notes,
          f"review-pending notification missing: {notes!r}")


# =======================================================================
# Sessions, goals, manifest (31-35)
# =======================================================================

FOOTER_GOAL = re.compile(r"^\[goal\] .+ \[active\]$")
FOOTER_TASKS = re.compile(r"^\[open tasks\] .+$")


def _check_footer(block: str, context: str):
    lines = footer_lines(block)
    check(len(lines) >= 2, f"{context}: block too short: {block!r}")
    check(FOOTER_GOAL.match(lines[-2]) or lines[-2] == "[goal] (none set)",
          f"{context}: second-to-last line is not the [goal] footer: "
          f"{lines[-2]!r}")
    check(FOOTER_TASKS.match(lines[-1]),
          f"{context}: last line is not the [open tasks] footer: "
          f"{lines[-1]!r}")
    return lines[-2], lines[-1]


def case_31(env: Env):
    # Goal/tasks set from the terminal side; ZERO hooks configured.
    env.write_settings({"hooks": {}})
    env.cli("goal", "--session", "s31", "set", "Ship the thing")
    env.cli("goal", "--session", "s31", "task-add", "alpha task")
    env.cli("goal", "--session", "s31", "task-add", "beta task")
    for i in (1, 2):
        proc = env.run_payload(
            f"```shell\nsession: s31\necho run-{i}\n```")
        check(proc.returncode == 0, proc.stderr)
        block = env.clip_text()
        goal_line, tasks_line = _check_footer(block, f"run {i}")
        check(goal_line == "[goal] Ship the thing [active]",
              f"run {i}: footer goal wrong: {goal_line!r}")
        check(tasks_line == "[open tasks] [open] alpha task · "
              "[open] beta task",
              f"run {i}: footer tasks wrong: {tasks_line!r}")
        artifact_check(env, proc, f"(case 31 run {i})")
    # Shape is stable and the footer updates when the state changes.
    env.cli("goal", "--session", "s31", "task-update", "alpha task",
            "done")
    env.run_payload("```shell\nsession: s31\necho run-3\n```")
    _, tasks_line = _check_footer(env.clip_text(), "run 3")
    check(tasks_line == "[open tasks] [open] beta task",
          f"footer did not update after task-done: {tasks_line!r}")


def case_32(env: Env):
    env.run_payload("```shell\nsession: s32\necho before\n```")
    check("[goal] (none set)" in env.clip_text(),
          "fresh session should show the empty footer")
    env.run_payload("```shell\nsession: s32\naction: goal-set\n"
                    "Migrate the jobs\n```")
    env.run_payload("```shell\nsession: s32\naction: task-add\n"
                    "first job\nsecond job\n```")
    env.run_payload("```shell\nsession: s32\necho after\n```")
    goal_line, tasks_line = _check_footer(env.clip_text(), "after ops")
    check(goal_line == "[goal] Migrate the jobs [active]",
          f"goal-set not reflected: {goal_line!r}")
    check("[open] first job" in tasks_line
          and "[open] second job" in tasks_line,
          f"task-add not reflected: {tasks_line!r}")
    env.run_payload("```shell\nsession: s32\naction: task-done\n"
                    "first job\n```")
    env.run_payload("```shell\nsession: s32\necho final\n```")
    _, tasks_line = _check_footer(env.clip_text(), "after task-done")
    check("first job" not in tasks_line
          and "second job" in tasks_line,
          f"task-done not reflected: {tasks_line!r}")


def case_33(env: Env):
    for sid in ("s33-a", "s33-b"):
        env.write_file(f"payload-{sid}.txt",
                       f"```shell\nsession: {sid}\ntimeout: 60\n"
                       f"sleep 30\n```")
    proc_a, out_a = env.bg("run", "--file", "payload-s33-a.txt")
    proc_b, out_b = env.bg("run", "--file", "payload-s33-b.txt")
    env.wait_for(lambda: env.session_state("s33-a") == "running"
                 and env.session_state("s33-b") == "running",
                 what="both runs to start")
    env.clip.write_text("```cancel id=s33-a```", encoding="utf-8")
    env.trigger()
    out = env.bg_finish(proc_a, out_a)
    check("[exit cancelled" in out, f"session A not cancelled: {out!r}")
    check(proc_b.poll() is None and env.session_state("s33-b") == "running",
          "cancelling session A disturbed session B")
    env.cli("cancel", "s33-b")  # cleanup
    out_b_text = env.bg_finish(proc_b, out_b)
    check("[exit cancelled" in out_b_text, "session B cleanup failed")


def case_34(env: Env):
    register = env.hook("register.py", """
import json, sys
data = json.load(sys.stdin)
cmd = (data.get("tool_input") or {}).get("command", "")
if "register" in cmd:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreRun",
        "registeredTools": [{"name": "demo.echo2",
                             "description": "Echo twice",
                             "shape": "```shell fence mentioning register"}]}}))
""")
    env.write_settings({"hooks": {"PreRun": [{"command": register}]}})
    proc = env.run_payload(
        "```shell\nsession: s34\necho register please\n```")
    check(proc.returncode == 0, proc.stderr)
    block = env.clip_text()
    check("— capabilities:" in block and "demo.echo2" in block,
          f"registered tool not in the refreshed manifest: {block!r}")
    tools = json.loads((env.state / "manifest-tools.json")
                       .read_text(encoding="utf-8"))
    check("demo.echo2" in tools, "registration not persisted")
    env.run_payload("```shell\nsession: s34\necho plain\n```")
    check("demo.echo2" not in env.clip_text(),
          "manifest refresh repeated after it was already sent")


def case_35(env: Env):
    env.run_payload("```shell\necho first\n```")
    check(env.events("ConfigChange") == [],
          "ConfigChange fired with no edit")
    env.write_settings({"allowlist": ["echo *"]})  # edit while armed
    env.run_payload("```shell\necho second\n```")
    changes = env.events("ConfigChange")
    check(changes and changes[-1]["data"].get("status") == "changed",
          f"settings edit not receipted as ConfigChange: {changes}")


# =======================================================================
# Trigger path (36)
# =======================================================================

def case_36(env: Env):
    env.clip.write_text('echo "hello world"', encoding="utf-8")
    proc = env.trigger()
    check(proc.returncode == 0, proc.stderr)
    block = env.clip_text()
    check(block.startswith("```result") and "hello world" in block,
          f"trigger did not run the clipboard: {block!r}")
    artifact_check(env, proc, "(case 36)")
    # Second invocation with the result on the clipboard: claimed —
    # the harness's own output is never re-executed.
    before = env.clip_text()
    proc2 = env.trigger()
    check(proc2.returncode == 0, proc2.stderr)
    check(env.clip_text() == before, "second trigger rewrote the clipboard")
    posts = [e for e in env.events("PostRun")
             if "hello world" in json.dumps(e["data"])]
    check(len(posts) == 1, f"double-fire: {len(posts)} executions")


# =======================================================================
# Inline diff rendering (37-41)
# =======================================================================

def case_37(env: Env):
    env.write_file("src/app.py", "print('v1')\n")
    cid = _stage(env, "```file\npath: src/app.py\nop: update\n"
                 "print('v2')\n```")
    show = env.cli("candidate", "show", cid)
    text = show.stdout
    check("src/app.py · UPDATE" in text, f"manifest line missing: {text!r}")
    check("src/app.py — UPDATE" in text, "diff heading (path + tag) missing")
    check("-print('v1')" in text and "+print('v2')" in text,
          f"diff lines missing: {text!r}")
    check(text.index("src/app.py · UPDATE") < text.index("-print('v1')"),
          "manifest must come before the diffs")
    proc = env.cli("candidate", "review", cid, "--decision", "accept")
    check(proc.returncode == 0, proc.stderr)
    block = env.clip_text()
    check("-print('v1')" in block and "+print('v2')" in block,
          "applied diff missing from the result block")


def case_38(env: Env):
    cid = _stage(env, "```file\npath: fresh.txt\nop: create\n"
                 "brand new content\n```")
    proc = env.cli("candidate", "review", cid, "--decision", "accept")
    check(proc.returncode == 0, proc.stderr)
    block = env.clip_text()
    check("✓ fresh.txt — NEW" in block, f"NEW tag missing: {block!r}")
    check("--- /dev/null" in block, "create not rendered from /dev/null")
    check("+brand new content" in block, "addition lines missing")
    check("-brand" not in block, "a create must not render removals")


def case_39(env: Env):
    content = "".join(f"row-{i:03d}\n" for i in range(300))
    cid = _stage(env, f"```file\npath: big.txt\n{content}```")
    proc = env.cli("candidate", "review", cid, "--decision", "accept")
    check(proc.returncode == 0, proc.stderr)
    block = env.clip_text()
    check("more lines — full diff in" in block,
          f"oversized diff not capped with a pointer: {block[:300]!r}")
    check("row-299" not in block, "oversized diff dumped in full")
    check("row-000" in block, "diff head missing")
    added = [ln for ln in block.splitlines() if "+row-" in ln]
    check(len(added) <= 85, f"per-file diff cap exceeded: {len(added)}")
    live = (env.proj / "big.txt").read_text().splitlines()
    check(len(live) == 300, "the cap truncated the actual file")


def case_40(env: Env):
    env.write_file("keep.txt", "old line\n")
    cid = _stage(env, "```file\npath: keep.txt\nop: update\nnew line\n```")
    proc = env.cli("candidate", "review", cid, "--decision", "reject",
                   "--reason", "not now")
    check(proc.returncode == 0, proc.stderr)
    text = env.clip_text()
    check("✗ keep.txt — rejected" in text,
          f"rejection marking missing: {text!r}")
    check("-old line" in text and "+new line" in text,
          f"rejected diff missing from clipboard text: {text!r}")
    check((env.proj / "keep.txt").read_text() == "old line\n",
          "rejected change was applied")
    artifact_check(env, proc, "(case 40)")


def case_41(env: Env):
    env.run_payload("```shell\nsession: s41\naction: goal-set\n"
                    "Diff positioning\n```")
    env.write_file("mod.py", "before\n")
    cid = _stage(env,
                 "```shell\nsession: s41\n```\n"
                 "```file\npath: mod.py\nop: update\nafter\n```")
    proc = env.cli("candidate", "review", cid, "--decision", "accept")
    check(proc.returncode == 0, proc.stderr)
    block = env.clip_text()
    check("+after" in block, "applied diff missing")
    check(block.index("+after") < block.index("[goal]"),
          "the diff must sit above the sticky footer")
    goal_line, _ = _check_footer(block, "case 41")
    check(goal_line == "[goal] Diff positioning [active]",
          f"footer wrong: {goal_line!r}")


# =======================================================================
# Execution modes (42-45)
# =======================================================================

def case_42(env: Env):
    recorder = env.hook("review_recorder.py", """
import json, os, sys
data = json.load(sys.stdin)
with open(os.environ["E2E_EVENT_LOG"], "a") as fh:
    fh.write(data.get("hook_event_name", "?") + ":" +
             str(data.get("review_status")) + "\\n")
""")
    env.write_settings({"hooks": {
        "ReviewDecision": [{"command": recorder}]}})
    log = env.root / "review-events.log"
    start = time.monotonic()
    proc = env.run_payload(
        "```file\npath: parked.txt\nparked\n```",
        timeout=15, env_over={"E2E_EVENT_LOG": str(log)})
    elapsed = time.monotonic() - start
    check(proc.returncode == 0, proc.stderr)
    check(elapsed < 10,
          f"async trigger blocked for {elapsed:.1f}s waiting on a "
          f"decision (fire-and-forget violated)")
    check("awaiting review" in env.clip_text(), "run did not park")
    cid = env.candidate_id()
    check((env.state / "candidates" / cid / "manifest.json").exists(),
          "candidate not staged")
    check(not (env.state / "candidates" / cid / "decision.json").exists(),
          "a decision materialised on its own")
    check(not (env.proj / "parked.txt").exists(),
          "parked candidate was applied without a decision")
    seen = log.read_text() if log.exists() else ""
    check("ReviewDecision:pending" in seen,
          f"pending notification did not fire: {seen!r}")


def case_43(env: Env):
    cid = _stage(env,
                 "```shell\nsession: s43\n```\n"
                 "```file\npath: later.txt\nlater\n```")
    decide = env.cli("candidate", "decide", cid, "--decision", "accept")
    check(decide.returncode == 0, decide.stderr)
    check(not (env.proj / "later.txt").exists(),
          "decide alone applied the candidate")
    proc = env.cli("candidate", "apply", cid)
    check(proc.returncode == 0, proc.stderr)
    check((env.proj / "later.txt").read_text() == "later\n",
          "decision delivered afterwards did not apply the candidate")
    applied = [e for e in env.events("ReviewDecision", run_id=cid)
               if e["data"].get("status") == "applied"]
    check(applied, "apply not receipted")
    check(env.events("PostRun", run_id=cid),
          "no PostRun receipt for the completed run")
    closed = [e for e in env.events("RunClosed", run_id=cid)
              if e["data"].get("status") == "closed"]
    check(closed, "run not closed after apply")
    check("✓ later.txt — NEW" in env.clip_text(),
          f"clipboard not updated with the result: {env.clip_text()!r}")
    check(env.session_state("s43") in ("completed", "closed"),
          f"session left in state {env.session_state('s43')!r}")


def case_44(env: Env):
    env.write_file("payload.txt",
                   "```shell\nattend: true\n```\n"
                   "```file\npath: att.txt\nop: create\nattended\n```")
    proc, out_path = env.bg(
        "run", "--file", "payload.txt",
        env_over={"CLIP_HARNESS_ATTEND_WAIT_S": "60"})
    cands = env.state / "candidates"
    env.wait_for(lambda: cands.exists()
                 and any((d / "manifest.json").exists()
                         for d in cands.iterdir()),
                 what="attended candidate to stage")
    time.sleep(1.5)
    check(proc.poll() is None,
          "attend:true run returned before any decision was given")
    cid = next(d.name for d in cands.iterdir()
               if (d / "manifest.json").exists())
    (cands / cid / "decision.json").write_text(json.dumps(
        {"decision": "accept", "reason": "", "accepted_files": None}),
        encoding="utf-8")
    out = env.bg_finish(proc, out_path, timeout=20)
    check((env.proj / "att.txt").read_text() == "attended\n",
          "attended decision was not applied inline")
    check("att.txt · NEW" in out and "+attended" in out,
          f"review surface not presented in the attended terminal: "
          f"{out!r}")
    check("✓ att.txt — NEW" in out,
          f"attended run did not complete inline: {out!r}")


def case_45(env: Env):
    proc = env.run_payload("```shell\necho plain-command\n```")
    check(proc.returncode == 0, proc.stderr)
    check("[exit 0" in env.clip_text(), "plain command did not complete")
    check(env.events("ReviewDecision") == [],
          "a plain command entered the review flow")
    listing = env.cli("candidate", "list")
    check("no pending candidates" in listing.stdout,
          f"a plain command parked a candidate: {listing.stdout!r}")


def case_46(env: Env):
    # M365 Copilot's copy button strips the ``` fence: the block's
    # contents alone must parse as the payload (the acceptance case).
    proc = env.run_payload(
        "session: clip-harness-smoke\n"
        "timeout: 30\n"
        "printf 'clip_harness_smoke=ok\\n'")
    check(proc.returncode == 0, proc.stderr)
    block = env.clip_text()
    check("clip_harness_smoke=ok" in block,
          f"unfenced body did not run: {block!r}")
    check("[exit 0" in block, f"unfenced run not clean: {block!r}")
    check("command not found" not in block,
          f"metadata lines reached the shell: {block!r}")
    check("[goal]" in block and "[open tasks]" in block,
          f"bound footer missing on unfenced run: {block!r}")
    artifact_check(env, proc, "(case 46)")
    # Unfenced goal/task ops bind the same session; the next unfenced
    # run's footer must show that session's goal and tasks.
    proc = env.run_payload(
        "session: clip-harness-smoke\n"
        "action: goal-set\n"
        "Smoke the unfenced form")
    check(proc.returncode == 0, proc.stderr)
    proc = env.run_payload(
        "session: clip-harness-smoke\n"
        "action: task-add\n"
        "Check the footer")
    check(proc.returncode == 0, proc.stderr)
    proc = env.run_payload(
        "session: clip-harness-smoke\n"
        "printf 'again=ok\\n'")
    check(proc.returncode == 0, proc.stderr)
    block = env.clip_text()
    check("[goal] Smoke the unfenced form [active]" in block,
          f"footer goal wrong after unfenced ops: {block!r}")
    check("[open tasks] [open] Check the footer" in block,
          f"footer tasks wrong after unfenced ops: {block!r}")
    # Malformed metadata fails closed: error note, nothing executed.
    proc = env.run_payload("session: s46\ntimeout: soon\necho never")
    check(proc.returncode == 0, proc.stderr)
    check(env.clip_text().startswith("⛔ clip-harness: malformed payload"),
          f"malformed unfenced not rejected: {env.clip_text()!r}")
    rejected = env.events("PayloadCaptured")[-1]
    check(rejected["data"]["status"] == "rejected",
          f"malformed run not receipted as rejected: {rejected}")
    check(env.events("PostRun", run_id=rejected["run_id"]) == [],
          "malformed unfenced payload produced a PostRun")


def case_47(env: Env):
    # result_budget_chars is a setting, not a constant: a 2k budget
    # elides a 12k output to a digest near 2k, the full output still
    # lands in the run's spill file, and the receipt records it.
    env.write_settings({"result_budget_chars": 2000})
    env.write_file("gen2.py",
                   "for i in range(600):\n"
                   "    print(f'row {i:05d} ' + 'x' * 9)\n")
    proc = env.run_payload("```shell\npython3 gen2.py\n```", timeout=60)
    check(proc.returncode == 0, proc.stderr)
    block = env.clip_text()
    match = re.search(
        r"\[output elided: (\d+) of 12000 chars shown · "
        r"full output \(12000 chars\) saved at (\S+)", block)
    check(match, f"no elision marker in block: {block[:200]!r}")
    shown = int(match.group(1))
    check(0 < shown < 2000, f"shown count ignores the budget: {shown}")
    check(len(block) < 3000,
          f"block not bounded near the custom budget: {len(block)}")
    check("row 00000" in block and "row 00599" in block,
          "digest is missing the head or the tail")
    check("row 00300" not in block, "elided middle leaked into the block")
    spilled = Path(match.group(2))
    check(spilled.exists() and spilled.stat().st_size == 12000,
          "full output not preserved in the spill file")
    posts = env.events("PostRun")
    check(posts and posts[-1]["data"].get("output_elided") is True,
          f"receipt does not record the elision: {posts[-1:]!r}")
    check(posts[-1]["data"].get("output_chars") == 12000,
          "receipt does not record the full output size")


def case_48(env: Env):
    # M365's copy button strips the ```file / ```patch fences too:
    # the fence contents standing alone stage as the same candidate
    # a fenced payload would, and review applies them identically.
    cid = _stage(env, "path: loose.txt\nop: create\nfence-free\n")
    entry = env.manifest(cid)["entries"][0]
    check(entry["declared_op"] == "create"
          and entry["actual_op"] == "NEW",
          f"unfenced file staged wrong: {entry}")
    proc = env.cli("candidate", "review", cid, "--decision", "accept")
    check(proc.returncode == 0, proc.stderr)
    check((env.proj / "loose.txt").read_text() == "fence-free\n",
          "unfenced file was not applied on accept")
    check("✓ loose.txt — NEW" in env.clip_text(),
          f"NEW tag missing for unfenced file: {env.clip_text()!r}")
    # Unfenced patch on the now-live file.
    cid = _stage(env,
                 "path: loose.txt\n--- a/loose.txt\n+++ b/loose.txt\n"
                 "@@ -1 +1 @@\n-fence-free\n+patched\n")
    proc = env.cli("candidate", "review", cid, "--decision", "accept")
    check(proc.returncode == 0, proc.stderr)
    check((env.proj / "loose.txt").read_text() == "patched\n",
          "unfenced patch was not applied on accept")
    # Fail closed: `path:` with neither an op: line nor a diff header
    # is a malformed payload — rejected, never run as bare shell.
    proc = env.run_payload("path: nope.txt\njust prose, no op\n")
    check(proc.returncode == 0, proc.stderr)
    check(env.clip_text().startswith("⛔ clip-harness: malformed payload"),
          f"ambiguous unfenced file not rejected: {env.clip_text()!r}")
    check(not (env.proj / "nope.txt").exists(),
          "rejected unfenced payload wrote a file")
    # Traversal is rejected at staging for the unfenced form too.
    proc = env.run_payload("path: ../escape.txt\nop: create\nx\n")
    check(proc.returncode == 0, proc.stderr)
    check("candidate staging failed" in env.clip_text(),
          f"unfenced traversal not rejected: {env.clip_text()!r}")
    check(not (env.root / "escape.txt").exists(),
          "unfenced traversal wrote outside the project")


def case_49(env: Env):
    # Acceptance (filed issue): the exact M365 copy-button clipboard,
    # driven through the zero-argument clipboard entry point — stage,
    # review, accept, and the file lands byte-exact, reported NEW.
    target = Path("/tmp/clip-harness-m365-file-smoke.txt")
    target.unlink(missing_ok=True)
    env.clip.write_text(
        "path: /tmp/clip-harness-m365-file-smoke.txt\n"
        "op: create\n"
        "clip_harness_file_review=ok", encoding="utf-8")
    proc = env.trigger()
    check(proc.returncode == 0, proc.stderr)
    check("awaiting review" in env.clip_text(),
          f"unfenced acceptance payload did not park: "
          f"{env.clip_text()!r}")
    cid = env.candidate_id()
    entry = env.manifest(cid)["entries"][0]
    check(entry["path"] == "/tmp/clip-harness-m365-file-smoke.txt"
          and entry["actual_op"] == "NEW",
          f"acceptance candidate staged wrong: {entry}")
    proc = env.cli("candidate", "review", cid, "--decision", "accept")
    check(proc.returncode == 0, proc.stderr)
    check(target.read_text(encoding="utf-8")
          == "clip_harness_file_review=ok",
          f"accepted file content wrong: {target.read_bytes()!r}")
    block = env.clip_text()
    check("✓ /tmp/clip-harness-m365-file-smoke.txt — NEW" in block,
          f"result does not report the file as NEW: {block!r}")
    for text in (block, proc.stdout):
        check("path:: command not found" not in text
              and "op:: command not found" not in text,
              f"header lines reached the shell: {text!r}")
    target.unlink(missing_ok=True)


def case_50(env: Env):
    # Trigger context: a pure file payload (NO attend header — the
    # form an M365 copy produces) is decided in the SAME invocation
    # when the decision lands in the window: no parking, no separate
    # review call. The headless decision channel is the file drop.
    env.clip.write_text(
        "```file\npath: inline.txt\nop: create\ninline-body\n```",
        encoding="utf-8")
    proc, out_path = env.bg(
        env_over={"CLIP_HARNESS_ATTEND_WAIT_S": "60"})
    cands = env.state / "candidates"
    env.wait_for(lambda: cands.exists()
                 and any((d / "manifest.json").exists()
                         for d in cands.iterdir()),
                 what="trigger candidate to stage")
    time.sleep(1.0)
    check(proc.poll() is None,
          "trigger returned before the inline decision was given")
    cid = next(d.name for d in cands.iterdir()
               if (d / "manifest.json").exists())
    (cands / cid / "decision.json").write_text(json.dumps(
        {"decision": "accept", "reason": "", "accepted_files": None}),
        encoding="utf-8")
    out = env.bg_finish(proc, out_path, timeout=20)
    check((env.proj / "inline.txt").read_text() == "inline-body\n",
          "inline trigger decision was not applied")
    check("✓ inline.txt — NEW" in out,
          f"trigger run did not complete inline: {out!r}")
    check("awaiting review" not in env.clip_text(),
          f"run parked instead of completing inline: "
          f"{env.clip_text()!r}")
    check(env.clip_text().startswith("```result"),
          f"clipboard holds no result block: {env.clip_text()!r}")


# =======================================================================
# Patch targeting (51-56): `path:` is authoritative — the diff's
# ---/+++ headers are checked against it and normalized to it, so a
# patch stages and applies from ANY trigger cwd.
# =======================================================================

_HELM_BODY = ("@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three\n")


def _trigger_stage(env: Env, clipboard_text: str) -> str:
    env.clip.write_text(clipboard_text, encoding="utf-8")
    proc = env.trigger()
    check(proc.returncode == 0, proc.stderr)
    check("awaiting review" in env.clip_text(),
          f"payload did not park: {env.clip_text()!r}")
    return env.candidate_id()


def case_51(env: Env):
    # (a) Absolute path: + repo-relative a//b/ headers, unfenced,
    # through the zero-arg trigger standing in another directory.
    target = env.root / "repo" / "suppliers" / "helm" / "normalize"
    target.parent.mkdir(parents=True)
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    sibling = target.parent / "sibling.txt"
    sibling.write_text("leave me\n", encoding="utf-8")
    cid = _trigger_stage(env,
                         f"path: {target}\n"
                         "--- a/suppliers/helm/normalize\n"
                         "+++ b/suppliers/helm/normalize\n"
                         + _HELM_BODY)
    entry = env.manifest(cid)["entries"][0]
    check(entry["path"] == str(target)
          and entry["abs_path"] == str(target)
          and entry["actual_op"] == "UPDATE",
          f"candidate does not name the authoritative target: {entry}")
    show = env.cli("candidate", "show", cid)
    check(str(target) in show.stdout and "+TWO" in show.stdout,
          f"review does not show the target and hunk: {show.stdout!r}")
    proc = env.cli("candidate", "review", cid, "--decision", "accept")
    check(proc.returncode == 0, proc.stderr)
    check(target.read_text(encoding="utf-8") == "one\nTWO\nthree\n",
          "patch not applied to the absolute target")
    check(sibling.read_text(encoding="utf-8") == "leave me\n",
          "a file other than the target was modified")
    check(f"✓ {target} — UPDATED" in env.clip_text(),
          f"result does not name the final target: {env.clip_text()!r}")


def case_52(env: Env):
    # (b) Absolute path: + basename-only headers.
    target = env.root / "elsewhere" / "normalize"
    target.parent.mkdir(parents=True)
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    cid = _trigger_stage(env,
                         f"path: {target}\n"
                         "--- normalize\n"
                         "+++ normalize\n"
                         + _HELM_BODY)
    proc = env.cli("candidate", "review", cid, "--decision", "accept")
    check(proc.returncode == 0, proc.stderr)
    check(target.read_text(encoding="utf-8") == "one\nTWO\nthree\n",
          "basename-header patch not applied to the target")
    check(f"✓ {target} — UPDATED" in env.clip_text(),
          f"result does not name the final target: {env.clip_text()!r}")


def case_53(env: Env):
    # (c) The acceptance case, verbatim shape: arbitrary cwd,
    # path: /tmp/.../project/file.txt, headers a/file.txt b/file.txt,
    # one hunk. Stages, review names the target, accept modifies
    # only that file.
    base = Path("/tmp/clip-harness-patch-acceptance")
    target = base / "project" / "file.txt"
    target.unlink(missing_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    try:
        cid = _trigger_stage(
            env,
            f"path: {target}\n"
            "--- a/file.txt\n"
            "+++ b/file.txt\n"
            "@@ -1,3 +1,3 @@\n alpha\n-beta\n+BETA\n gamma\n")
        show = env.cli("candidate", "show", cid)
        check(str(target) in show.stdout and "+BETA" in show.stdout,
              f"review does not show final path + hunk: {show.stdout!r}")
        proc = env.cli("candidate", "review", cid,
                       "--decision", "accept")
        check(proc.returncode == 0, proc.stderr)
        check(target.read_text(encoding="utf-8")
              == "alpha\nBETA\ngamma\n",
              "acceptance patch not applied")
        check(f"✓ {target} — UPDATED" in env.clip_text(),
              f"result does not identify the final target: "
              f"{env.clip_text()!r}")
    finally:
        target.unlink(missing_ok=True)
        try:
            target.parent.rmdir()
            base.rmdir()
        except OSError:
            pass


def case_54(env: Env):
    # (d) Headers naming a different file than path: — staging
    # fails closed: no candidate parks, the target is untouched.
    target = env.root / "repo" / "file.txt"
    target.parent.mkdir(parents=True)
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    env.clip.write_text(
        f"path: {target}\n"
        "--- a/other.txt\n"
        "+++ b/other.txt\n"
        + _HELM_BODY, encoding="utf-8")
    proc = env.trigger()
    check(proc.returncode == 0, proc.stderr)
    block = env.clip_text()
    check("candidate staging failed" in block
          and "different file" in block,
          f"disagreeing patch not refused at staging: {block!r}")
    cands = env.state / "candidates"
    check(not cands.exists()
          or not any(cands.rglob("manifest.json")),
          "a candidate parked for a refused patch")
    check(target.read_text(encoding="utf-8") == "one\ntwo\nthree\n",
          "refused patch touched the target")


def case_55(env: Env):
    # (e) Target moves between staging and acceptance — the
    # base-hash check refuses the apply, as for any mutation.
    target = env.root / "repo" / "file.txt"
    target.parent.mkdir(parents=True)
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    cid = _trigger_stage(env,
                         f"path: {target}\n"
                         "--- a/file.txt\n"
                         "+++ b/file.txt\n"
                         + _HELM_BODY)
    target.write_text("moved\ntwo\nthree\n", encoding="utf-8")
    proc = env.cli("candidate", "review", cid, "--decision", "accept")
    check(proc.returncode == 0, proc.stderr)
    check(target.read_text(encoding="utf-8") == "moved\ntwo\nthree\n",
          "stale target was overwritten by the apply")
    check("FAILED" in env.clip_text()
          and "changed on disk since staging" in env.clip_text(),
          f"stale apply not refused in the result: {env.clip_text()!r}")


def case_56(env: Env):
    # (f) The fenced form of (a): same absolute target, same
    # repo-relative headers, inside a ```patch fence.
    target = env.root / "repo" / "suppliers" / "helm" / "normalize"
    target.parent.mkdir(parents=True)
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    cid = _trigger_stage(env,
                         f"```patch\npath: {target}\n"
                         "--- a/suppliers/helm/normalize\n"
                         "+++ b/suppliers/helm/normalize\n"
                         + _HELM_BODY + "```")
    entry = env.manifest(cid)["entries"][0]
    check(entry["abs_path"] == str(target)
          and entry["actual_op"] == "UPDATE",
          f"fenced patch staged against the wrong target: {entry}")
    proc = env.cli("candidate", "review", cid, "--decision", "accept")
    check(proc.returncode == 0, proc.stderr)
    check(target.read_text(encoding="utf-8") == "one\nTWO\nthree\n",
          "fenced patch not applied to the absolute target")


# =======================================================================
# Matrix runner
# =======================================================================

CASES = [(n, fn) for n, fn in sorted(globals().items())
         if re.fullmatch(r"case_\d+", n)]


def main() -> int:
    wanted = {int(a) for a in sys.argv[1:] if a.isdigit()}
    failures = 0
    for name, fn in CASES:
        number = int(name.split("_")[1])
        if wanted and number not in wanted:
            continue
        env = Env()
        try:
            fn(env)
        except Exception as exc:  # noqa: BLE001 - report and continue
            failures += 1
            print(f"CASE {number:02d}: FAIL — {exc}")
        else:
            print(f"CASE {number:02d}: PASS")
    total = len(wanted) if wanted else len(CASES)
    print(f"\n{total - failures}/{total} cases passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
