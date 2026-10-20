"""file.edit tests: the ```edit fence (Claude Code's Edit pattern),
its gate transform, and the malformed-patch hunk-count diagnostics.

The model on the other side of the clipboard kept failing
patch.apply because a hand-written unified diff needs hand-counted
@@ hunk counts. The edit fence is the fix: the model supplies exact
old/new text, the gate computes the candidate bytes from the live
file, and the harness generates the review diff — the model never
counts hunks for a change it can express as an edit.
"""

import json
import sys
from pathlib import Path

import pytest

from clip_harness.adapters.headless import StaticDecider
from clip_harness.core import FileSpec, RunState
from clip_harness.gate import (DecisionRecord, GateError,
                               apply_candidate, classify_and_stage,
                               render_diff)
from clip_harness.manifest import BUILTIN_TOOLS
from clip_harness.payload import (PayloadError, parse_clipboard_text,
                                  parse_payload)
from clip_harness.settings import HookHandler, Settings

from conftest import FakeExecutor


def _stage(tmp_path: Path, specs, workdir: Path):
    return classify_and_stage(tmp_path / "cand", "r-1", None, specs, workdir)


def _work(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    return work


EDIT_FENCE = ("```edit\npath: a.txt\nold_string:\nold line\n"
              "new_string:\nnew line\n```")


# -- parser: the fence ------------------------------------------------------

def test_edit_fence_canonical_form():
    payload = parse_payload(
        "```edit\npath: src/auth.py\nreplace_all: false\n"
        "old_string:\ndef old():\n    return 1\n"
        "new_string:\ndef new():\n    return 2\n```")
    assert payload.pure_mutation is True
    spec = payload.files[0]
    assert spec.is_edit is True
    assert spec.is_patch is False
    assert spec.declared_op == "update"  # an edit is always an update
    assert spec.replace_all is False
    # Sections are verbatim, joined with "\n", no trailing newline.
    assert spec.old_string == "def old():\n    return 1"
    assert spec.new_string == "def new():\n    return 2"


def test_edit_fence_replace_all_header():
    payload = parse_payload(
        "```edit\npath: a.txt\nreplace_all: true\n"
        "old_string:\nx\nnew_string:\ny\n```")
    assert payload.files[0].replace_all is True
    # Absent header = the default, false.
    payload = parse_payload(EDIT_FENCE)
    assert payload.files[0].replace_all is False


def test_edit_fence_empty_new_string_is_a_delete():
    payload = parse_payload(
        "```edit\npath: a.txt\nold_string:\ngone\nnew_string:\n```")
    spec = payload.files[0]
    assert spec.new_string == ""
    assert spec.is_edit is True


def test_edit_marker_lines_must_be_exact():
    # A line that merely STARTS with a marker name is content, not a
    # delimiter — only a line that is exactly `new_string:` delimits.
    payload = parse_payload(
        "```edit\npath: a.txt\nold_string:\nnew_string: not a marker\n"
        "new_string:\nreplacement\n```")
    spec = payload.files[0]
    assert spec.old_string == "new_string: not a marker"
    assert spec.new_string == "replacement"


def test_edit_fence_requires_path_first():
    with pytest.raises(PayloadError):
        parse_payload("```edit\nold_string:\nx\nnew_string:\ny\n```")


def test_edit_empty_old_string_fails_closed():
    with pytest.raises(PayloadError, match="empty old_string"):
        parse_payload(
            "```edit\npath: a.txt\nold_string:\nnew_string:\ny\n```")


def test_edit_missing_markers_fail_closed():
    with pytest.raises(PayloadError, match="new_string"):
        parse_payload("```edit\npath: a.txt\nold_string:\nx\n```")
    with pytest.raises(PayloadError, match="old_string"):
        parse_payload("```edit\npath: a.txt\nnew_string:\ny\n```")


def test_edit_op_line_fails_closed():
    # The edit grammar has no op: an `op:` line is malformed, never
    # silently reinterpreted as a file fence.
    with pytest.raises(PayloadError, match="op:"):
        parse_payload(
            "```edit\npath: a.txt\nop: update\nold_string:\nx\n"
            "new_string:\ny\n```")


def test_edit_replace_all_invalid_value_fails_closed():
    with pytest.raises(PayloadError, match="replace_all"):
        parse_payload(
            "```edit\npath: a.txt\nreplace_all: maybe\n"
            "old_string:\nx\nnew_string:\ny\n```")


def test_edit_fence_mixed_with_shell_fence():
    payload = parse_payload(
        "```shell\nsession: s1\nmake test\n```\n" + EDIT_FENCE)
    assert payload.command == "make test"
    assert payload.session_id == "s1"
    assert len(payload.files) == 1
    assert payload.files[0].is_edit is True
    assert payload.pure_mutation is False


def test_malformed_edit_never_becomes_bare_text():
    # Fenced or not, a broken edit fails closed — it never falls
    # through the ladder to the bare-text rung.
    for text in ("```edit\npath: a.txt\nold_string:\nx\n```",
                 "path: a.txt\nold_string:\nx\n"):
        with pytest.raises(PayloadError):
            parse_clipboard_text(text, allow_bare=True)


# -- parser: the unfenced form (fence-stripped copies) -----------------------

def test_unfenced_edit_parses():
    payload = parse_clipboard_text(
        "path: a.txt\nold_string:\nfoo\nnew_string:\nbar\n",
        allow_bare=True)
    spec = payload.files[0]
    assert spec.is_edit is True
    assert spec.old_string == "foo"
    assert spec.new_string == "bar"
    assert spec.declared_op == "update"
    # Recognised on the watch path too (allow_bare=False).
    payload = parse_clipboard_text(
        "path: a.txt\nreplace_all: true\nold_string:\nfoo\n"
        "new_string:\nbar\n", allow_bare=False)
    assert payload.files[0].is_edit is True
    assert payload.files[0].replace_all is True


def test_unfenced_edit_malformed_fails_closed():
    for text in ("path: a.txt\nold_string:\nfoo\n",  # no new_string:
                 "path: a.txt\nreplace_all: maybe\nold_string:\nx\n"
                 "new_string:\ny\n",
                 "path: a.txt\nold_string:\nnew_string:\ny\n"):
        with pytest.raises(PayloadError):
            parse_clipboard_text(text, allow_bare=True)
        with pytest.raises(PayloadError):
            parse_clipboard_text(text, allow_bare=False)


# -- gate: the edit transform --------------------------------------------------

def test_edit_stages_and_applies_with_harness_diff(tmp_path):
    work = _work(tmp_path)
    (work / "a.txt").write_text("one\ntwo\nthree\n")
    spec = FileSpec(path="a.txt", old_string="two", new_string="TWO")
    candidate = _stage(tmp_path, [spec], work)
    entry = candidate.entries[0]
    assert entry.actual_op == "UPDATE"
    assert entry.mismatch is False
    staged = (candidate.dir / "candidates" / entry.candidate_file)
    assert staged.read_text() == "one\nTWO\nthree\n"
    assert (work / "a.txt").read_text() == "one\ntwo\nthree\n"
    # The review diff is harness-generated from the computed bytes.
    diff = render_diff(candidate, entry)
    assert "-two" in diff and "+TWO" in diff
    report = apply_candidate(candidate, DecisionRecord(decision="accept"))
    assert report.ok
    assert (work / "a.txt").read_text() == "one\nTWO\nthree\n"


def test_edit_insert_via_anchor_in_new_string(tmp_path):
    work = _work(tmp_path)
    (work / "a.txt").write_text("first\nlast\n")
    spec = FileSpec(path="a.txt", old_string="first",
                    new_string="first\ninserted")
    candidate = _stage(tmp_path, [spec], work)
    apply_candidate(candidate, DecisionRecord(decision="accept"))
    assert (work / "a.txt").read_text() == "first\ninserted\nlast\n"


def test_edit_delete_via_empty_new_string(tmp_path):
    work = _work(tmp_path)
    (work / "a.txt").write_text("keep\ndrop me\nkeep too\n")
    spec = FileSpec(path="a.txt", old_string="drop me\n", new_string="")
    candidate = _stage(tmp_path, [spec], work)
    apply_candidate(candidate, DecisionRecord(decision="accept"))
    assert (work / "a.txt").read_text() == "keep\nkeep too\n"


def test_edit_zero_matches_fails_and_leaves_file_untouched(tmp_path):
    work = _work(tmp_path)
    (work / "a.txt").write_text("actual content\n")
    spec = FileSpec(path="a.txt", old_string="not there",
                    new_string="replacement")
    with pytest.raises(GateError, match="not found"):
        _stage(tmp_path, [spec], work)
    assert (work / "a.txt").read_text() == "actual content\n"


def test_edit_multiple_matches_need_replace_all(tmp_path):
    work = _work(tmp_path)
    (work / "a.txt").write_text("dup\nmid\ndup\n")
    spec = FileSpec(path="a.txt", old_string="dup", new_string="uniq")
    with pytest.raises(GateError) as excinfo:
        _stage(tmp_path, [spec], work)
    message = str(excinfo.value)
    assert "2 places" in message  # the count is named
    assert "surrounding context" in message  # remedy one
    assert "replace_all: true" in message  # remedy two
    assert (work / "a.txt").read_text() == "dup\nmid\ndup\n"
    # With replace_all, every occurrence is replaced.
    spec = FileSpec(path="a.txt", old_string="dup", new_string="uniq",
                    replace_all=True)
    candidate = _stage(tmp_path, [spec], work)
    report = apply_candidate(candidate, DecisionRecord(decision="accept"))
    assert report.ok
    assert (work / "a.txt").read_text() == "uniq\nmid\nuniq\n"


def test_edit_old_equals_new_fails(tmp_path):
    work = _work(tmp_path)
    (work / "a.txt").write_text("same\n")
    spec = FileSpec(path="a.txt", old_string="same", new_string="same")
    with pytest.raises(GateError, match="identical"):
        _stage(tmp_path, [spec], work)


def test_edit_missing_target_fails_toward_file_fence(tmp_path):
    work = _work(tmp_path)
    spec = FileSpec(path="ghost.txt", old_string="x", new_string="y")
    with pytest.raises(GateError) as excinfo:
        _stage(tmp_path, [spec], work)
    message = str(excinfo.value)
    assert "does not exist" in message
    assert "```file" in message  # creation lives in the file fence
    assert not (work / "ghost.txt").exists()


def test_edit_non_utf8_target_fails_closed(tmp_path):
    work = _work(tmp_path)
    (work / "bin.dat").write_bytes(b"\xff\xfe\x00binary")
    spec = FileSpec(path="bin.dat", old_string="x", new_string="y")
    with pytest.raises(GateError, match="UTF-8"):
        _stage(tmp_path, [spec], work)
    assert (work / "bin.dat").read_bytes() == b"\xff\xfe\x00binary"


def test_duplicate_edit_for_same_file_rejected(tmp_path):
    work = _work(tmp_path)
    (work / "a.txt").write_text("one\ntwo\n")
    edits = [FileSpec(path="a.txt", old_string="one", new_string="ONE"),
             FileSpec(path="a.txt", old_string="two", new_string="TWO")]
    with pytest.raises(GateError, match="one edit per file per payload"):
        _stage(tmp_path, edits, work)
    # An edit plus ANY other fence for the same path is rejected too.
    mixed = [FileSpec(path="a.txt", old_string="one", new_string="ONE"),
             FileSpec(path="a.txt", declared_op="update",
                      content="whole\n")]
    with pytest.raises(GateError, match="one edit per file per payload"):
        _stage(tmp_path, mixed, work)
    assert (work / "a.txt").read_text() == "one\ntwo\n"


def test_edit_shell_syntax_in_new_string_is_inert(tmp_path):
    work = _work(tmp_path)
    (work / "a.txt").write_text("anchor\n")
    payload_text = "$(touch pwned) `touch pwned2` > pwned3; rm -rf /"
    spec = FileSpec(path="a.txt", old_string="anchor",
                    new_string=payload_text)
    candidate = _stage(tmp_path, [spec], work)
    report = apply_candidate(candidate, DecisionRecord(decision="accept"))
    assert report.ok
    # The shell syntax landed as literal text — nothing executed it.
    assert (work / "a.txt").read_text() == payload_text + "\n"
    assert not (work / "pwned").exists()
    assert not (work / "pwned2").exists()
    assert not (work / "pwned3").exists()


# -- gate: malformed-patch hunk-count diagnostics ------------------------------

def test_patch_hunk_count_mismatch_names_declared_vs_actual(tmp_path):
    work = _work(tmp_path)
    (work / "f.txt").write_text("a\nb\nc\nd\ne\n")
    # Header declares -6/+10; the body actually has 5 old / 9 new.
    body = [" ctx1", " ctx2", " ctx3", " ctx4", "-old",
            "+n1", "+n2", "+n3", "+n4", "+n5"]
    patch = ("--- a/f.txt\n+++ b/f.txt\n@@ -1,6 +1,10 @@\n"
             + "\n".join(body) + "\n")
    with pytest.raises(GateError) as excinfo:
        _stage(tmp_path, [FileSpec(path="f.txt", patch=patch)], work)
    message = str(excinfo.value)
    assert "hunk 1" in message
    assert "-6/+10" in message  # declared
    assert "5 old / 9 new" in message  # actual
    assert "fix the @@ counts" in message  # remedy one
    assert "```edit" in message  # remedy two
    assert (work / "f.txt").read_text() == "a\nb\nc\nd\ne\n"


def test_patch_valid_multi_hunk_with_no_newline_marker_passes(tmp_path):
    # A realistic multi-hunk patch (context lines at the edges, a
    # "\ No newline" marker the validator must ignore) passes the
    # diagnostics exactly and stages through git as before.
    work = _work(tmp_path)
    (work / "f.txt").write_text("1\n2\n3\n4\n5\n6\n7\n8\n9")
    patch = ("--- a/f.txt\n+++ b/f.txt\n"
             "@@ -1,3 +1,3 @@\n 1\n-2\n+TWO\n 3\n"
             "@@ -7,3 +7,3 @@\n 7\n-8\n+EIGHT\n 9\n"
             "\\ No newline at end of file\n")
    candidate = _stage(tmp_path, [FileSpec(path="f.txt", patch=patch)],
                       work)
    entry = candidate.entries[0]
    assert entry.actual_op == "UPDATE"
    staged = (candidate.dir / "candidates" / entry.candidate_file)
    assert staged.read_text() == "1\nTWO\n3\n4\n5\n6\n7\nEIGHT\n9"


# -- runner: hook dispatch + never-shell ---------------------------------------

def test_edit_dispatches_claude_code_edit_hook_shape(
        make_runner, hook_script, tmp_path):
    work = _work(tmp_path)
    (work / "a.txt").write_text("old line\n")
    record = tmp_path / "hooks.jsonl"
    body = ("import json, sys\n"
            "data = json.load(sys.stdin)\n"
            f"open({str(record)!r}, 'a').write(json.dumps(data) + '\\n')\n")
    command = hook_script("record.py", body)
    settings = Settings(state_dir=tmp_path / "state",
                        hooks={"PreRun": [HookHandler(command=command)]})
    runner = make_runner(settings=settings)
    outcome = runner.process_text(EDIT_FENCE, str(work))
    assert outcome.state == RunState.PENDING_REVIEW
    seen = [json.loads(line) for line in record.read_text().splitlines()]
    pre = [d for d in seen if d["hook_event_name"] == "PreRun"]
    assert len(pre) == 1
    assert pre[0]["tool_name"] == "Edit"
    # Claude Code's Edit input shape, exactly these four keys.
    assert pre[0]["tool_input"] == {
        "file_path": "a.txt", "old_string": "old line",
        "new_string": "new line", "replace_all": False}


def test_edit_payload_never_reaches_the_shell_executor(
        make_runner, tmp_path):
    work = _work(tmp_path)
    (work / "a.txt").write_text("old line\n")
    executor = FakeExecutor()
    runner = make_runner(executor=executor, decider=None)
    outcome = runner.process_text(EDIT_FENCE, str(work))
    assert outcome.state == RunState.PENDING_REVIEW
    done = runner.review_candidate(
        outcome.run_id, StaticDecider(DecisionRecord(decision="accept")))
    assert done.state == RunState.COMPLETED
    assert (work / "a.txt").read_text() == "new line\n"
    assert "✓ a.txt — UPDATED" in done.result_block
    assert executor.calls == []  # the replacement text is data


def test_malformed_edit_fence_never_reaches_shell(
        make_runner, clipboard):
    executor = FakeExecutor()
    runner = make_runner(executor=executor)
    outcome = runner.process_text(
        "```edit\npath: a.txt\nop: update\nold_string:\nx\n"
        "new_string:\ny\n```", "/tmp", allow_bare=True)
    assert outcome.state == RunState.DENIED
    assert clipboard.text.startswith("⛔ clip-harness: malformed payload")
    assert executor.calls == []


# -- manifest --------------------------------------------------------------------

def test_manifest_lists_file_edit_between_write_and_patch():
    names = [tool["name"] for tool in BUILTIN_TOOLS]
    assert names.index("file.write") < names.index("file.edit") \
        < names.index("patch.apply")
