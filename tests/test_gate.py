"""Gate tests: built-in policy ordering and the §7.3 candidate flow."""

from pathlib import Path

import pytest

from clip_harness.core import Decision, FileSpec
from clip_harness.gate import (DecisionRecord, apply_candidate,
                               classify_and_stage, evaluate_policy,
                               looks_like_mutation)
from clip_harness.settings import Settings


def settings(**kwargs) -> Settings:
    return Settings(**kwargs)


# -- built-in policy ------------------------------------------------------

def test_hard_block_beats_allowlist():
    s = settings(allowlist=["rm *"], hard_block_patterns=[r"rm\s+-rf\s+/"])
    verdict = evaluate_policy("rm -rf /", s)
    assert verdict.hard_block is True
    assert verdict.decision == Decision.DENY


def test_denylist_denies():
    verdict = evaluate_policy("sudo ls", settings(denylist=["sudo *"]))
    assert verdict.decision == Decision.DENY


def test_allowlist_miss_asks():
    verdict = evaluate_policy("make test", settings(allowlist=["git *"]))
    assert verdict.decision == Decision.ASK


def test_allowlist_hit_allows():
    verdict = evaluate_policy("git status", settings(allowlist=["git *"]))
    assert verdict.decision == Decision.ALLOW


def test_mutation_heuristic_asks_for_review():
    verdict = evaluate_policy("cp a b", settings())
    assert verdict.decision == Decision.ASK


def test_readonly_command_allows_with_defaults():
    verdict = evaluate_policy("ls -la", settings())
    assert verdict.decision == Decision.ALLOW


def test_mutation_gate_deny_setting():
    verdict = evaluate_policy("touch x", settings(mutation_gate="deny"))
    assert verdict.decision == Decision.DENY


def test_protected_path_mutation_hard_blocks():
    s = settings(protected_paths=[".env"])
    verdict = evaluate_policy("cp .env .env.bak", s)
    assert verdict.hard_block is True


def test_protected_path_read_is_not_blocked():
    s = settings(protected_paths=[".env"], allowlist=["cat *"])
    verdict = evaluate_policy("cat .env", s)
    assert verdict.hard_block is False


# -- mutation heuristic: quoted text is data, not shell syntax -----------
#
# Regression: a read-only inspection command (ps/awk process filter,
# wc / shasum / `bash -n` over two scripts) was held as "mutates
# files" because the raw scan read the awk comparison `NR>1` inside
# quotes as a shell redirect. The scan now runs over a quote-masked
# copy: quoted contents are blanked, real shell syntax still shows.

READONLY_INSPECTION = (
    "ps -eo pid,args | awk 'NR>1 && /helm/ {print $1}'\n"
    "awk '/normalize/ {found=1} "
    "END {if (!found) print \"helm_normalizer=stopped\"}' procs.txt\n"
    "printf '%s\\n' '=== IMPLEMENTATION IDENTITIES ==='\n"
    "for file in suppliers/helm/discover suppliers/helm/normalize; do\n"
    "  if test -f \"$file\"; then\n"
    "    printf 'lines=%s\\n' \"$(wc -l < \"$file\" | tr -d ' ')\"\n"
    "    printf 'sha256=%s\\n' \"$(shasum -a 256 \"$file\" "
    "| awk '{print $1}')\"\n"
    "    /bin/bash -n \"$file\" && printf 'bash_syntax=valid\\n' "
    "|| printf 'bash_syntax=invalid\\n'\n"
    "  fi\n"
    "done\n")


def test_readonly_inspection_is_not_a_mutation():
    assert looks_like_mutation(READONLY_INSPECTION) is False
    verdict = evaluate_policy(READONLY_INSPECTION, settings())
    assert verdict.decision == Decision.ALLOW


@pytest.mark.parametrize("command", [
    "echo x > out.txt",
    "cmd >> log",
    "sed -i 's/a/b/' f",
    "cp a b",
    "rm -rf x",
    "echo hi > '/tmp/spaced name'",
    # An escaped quote opens no span: the redirect after it is real.
    "echo don\\'t > out",
])
def test_real_mutations_still_flagged(command):
    assert looks_like_mutation(command) is True


@pytest.mark.parametrize("command", [
    "grep 'a>b' f",
    "awk '$3 >= 2 {print}'",
    "printf '%s\\n' 'a > b'",
    'echo "results > expectations"',
    # An escaped quote inside double quotes ends no span.
    'echo "say \\"hi > there\\""',
    # An unterminated quote masks to the end of the string.
    "awk 'NR>1 {print}",
])
def test_quoted_metacharacters_are_not_mutations(command):
    assert looks_like_mutation(command) is False


def test_mask_quoted_preserves_positions():
    from clip_harness.gate import _mask_quoted
    masked = _mask_quoted("a 'b>c' \"d>e\" f")
    assert masked == "a '   ' \"   \" f"
    assert len(masked) == len("a 'b>c' \"d>e\" f")


# -- mutation heuristic: nested `sh -c` shells are seen through -----------

@pytest.mark.parametrize("command", [
    "bash -c 'rm x'",
    'bash -c "mv a b"',
    "sh -c 'echo hi > out'",
    "/bin/bash -lc 'touch x'",
    "bash -c 'bash -c \"rm x\"'",
])
def test_nested_shell_mutations_flagged(command):
    assert looks_like_mutation(command) is True


def test_nested_shell_quoted_comparison_not_flagged():
    assert looks_like_mutation("bash -c 'awk \"NR>1\"'") is False


# -- classification + staging ----------------------------------------------

def _stage(tmp_path: Path, specs, workdir: Path):
    return classify_and_stage(tmp_path / "cand", "r-1", None, specs, workdir)


def test_classify_new_update_delete_nochange(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "exists.txt").write_text("old\n")
    (work / "same.txt").write_text("same\n")
    (work / "gone.txt").write_text("bye\n")
    specs = [
        FileSpec(path="new.txt", declared_op="create", content="fresh\n"),
        FileSpec(path="exists.txt", declared_op="update", content="new\n"),
        FileSpec(path="gone.txt", declared_op="delete", content=""),
        FileSpec(path="same.txt", declared_op="update", content="same\n"),
    ]
    candidate = _stage(tmp_path, specs, work)
    ops = {e.path: e.actual_op for e in candidate.entries}
    assert ops == {"new.txt": "NEW", "exists.txt": "UPDATE",
                   "gone.txt": "DELETE", "same.txt": "NO_CHANGE"}
    # The no-op "update" is flagged: declared update, reality NO_CHANGE.
    mismatches = {e.path for e in candidate.entries if e.mismatch}
    assert mismatches == {"same.txt"}
    # Staging never writes live files.
    assert not (work / "new.txt").exists()
    assert (work / "exists.txt").read_text() == "old\n"
    assert (work / "gone.txt").exists()
    assert "NEW" in candidate.summary()


def test_declared_op_mismatch_is_flagged(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "real.txt").write_text("here\n")
    specs = [FileSpec(path="real.txt", declared_op="create",
                      content="changed\n"),
             FileSpec(path="ghost.txt", declared_op="update",
                      content="boo\n")]
    candidate = _stage(tmp_path, specs, work)
    by_path = {e.path: e for e in candidate.entries}
    assert by_path["real.txt"].actual_op == "UPDATE"
    assert by_path["real.txt"].mismatch is True
    assert by_path["ghost.txt"].actual_op == "NEW"
    assert by_path["ghost.txt"].mismatch is True


def test_patch_staging_via_git(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "f.txt").write_text("a\nb\nc\n")
    patch = ("--- a/f.txt\n+++ b/f.txt\n@@ -1,3 +1,3 @@\n"
             " a\n-b\n+B\n c\n")
    specs = [FileSpec(path="f.txt", declared_op="auto", patch=patch)]
    candidate = _stage(tmp_path, specs, work)
    entry = candidate.entries[0]
    assert entry.actual_op == "UPDATE"
    staged = (candidate.dir / "candidates" / entry.candidate_file)
    assert staged.read_text() == "a\nB\nc\n"
    assert (work / "f.txt").read_text() == "a\nb\nc\n"  # live untouched


def test_bad_patch_fails_closed(tmp_path):
    from clip_harness.gate import GateError
    work = tmp_path / "work"
    work.mkdir()
    (work / "f.txt").write_text("a\n")
    patch = "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-zzz\n+yyy\n"
    with pytest.raises(GateError):
        _stage(tmp_path, [FileSpec(path="f.txt", patch=patch)], work)


def test_traversal_target_rejected_at_staging(tmp_path):
    from clip_harness.gate import GateError
    work = tmp_path / "work"
    work.mkdir()
    for path in ("../escape.txt", "sub/../../escape.txt"):
        with pytest.raises(GateError):
            _stage(tmp_path, [FileSpec(path=path, declared_op="create",
                                       content="x\n")], work)
    # Nothing staged, nothing written outside the workdir.
    assert not (tmp_path / "escape.txt").exists()


def test_absolute_target_stages_at_its_named_path(tmp_path):
    # An absolute path is an explicit target, not a traversal: it
    # stages (behind the same review gate) at the named location.
    work = tmp_path / "work"
    work.mkdir()
    target = tmp_path / "elsewhere" / "named.txt"
    candidate = _stage(tmp_path, [FileSpec(
        path=str(target), declared_op="create", content="x\n")], work)
    entry = candidate.entries[0]
    assert entry.abs_path == str(target)
    assert entry.actual_op == "NEW"
    assert not target.exists()  # staging still writes nothing live


# -- apply -------------------------------------------------------------------

def _accepted_all():
    return DecisionRecord(decision="accept")


def test_apply_writes_only_after_accept(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    candidate = _stage(tmp_path, [FileSpec(path="a.txt", content="A\n")],
                       work)
    report = apply_candidate(candidate, _accepted_all())
    assert report.ok
    assert (work / "a.txt").read_text() == "A\n"
    assert report.entries[0].status == "applied"
    assert "NEW" in report.outcome_lines()[0]


def test_apply_per_file_acceptance(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    specs = [FileSpec(path="yes.txt", content="Y\n"),
             FileSpec(path="no.txt", content="N\n")]
    candidate = _stage(tmp_path, specs, work)
    record = DecisionRecord(decision="accept", accepted_files=["yes.txt"])
    report = apply_candidate(candidate, record)
    assert (work / "yes.txt").exists()
    assert not (work / "no.txt").exists()
    statuses = {e.path: e.status for e in report.entries}
    assert statuses == {"yes.txt": "applied", "no.txt": "rejected"}


def test_apply_delete_and_nochange(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "d.txt").write_text("x\n")
    (work / "s.txt").write_text("same\n")
    specs = [FileSpec(path="d.txt", declared_op="delete", content=""),
             FileSpec(path="s.txt", declared_op="update", content="same\n")]
    candidate = _stage(tmp_path, specs, work)
    report = apply_candidate(candidate, _accepted_all())
    assert not (work / "d.txt").exists()
    statuses = {e.path: e.status for e in report.entries}
    assert statuses == {"d.txt": "applied", "s.txt": "skipped"}


def test_accept_edited_applies_the_edited_candidate(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    candidate = _stage(tmp_path, [FileSpec(path="a.txt", content="v1\n")],
                       work)
    entry = candidate.entries[0]
    staged = candidate.dir / "candidates" / entry.candidate_file
    staged.write_text("v2 — edited in review\n")
    report = apply_candidate(candidate,
                             DecisionRecord(decision="accept-edited"))
    assert (work / "a.txt").read_text() == "v2 — edited in review\n"
    assert report.entries[0].reviewer_modified is True


def test_base_moved_since_staging_fails_and_rolls_back(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "one.txt").write_text("one\n")
    (work / "two.txt").write_text("two\n")
    specs = [FileSpec(path="one.txt", declared_op="update",
                      content="ONE\n"),
             FileSpec(path="two.txt", declared_op="update",
                      content="TWO\n")]
    candidate = _stage(tmp_path, specs, work)
    # The live tree moved under the candidate.
    (work / "two.txt").write_text("two — edited elsewhere\n")
    report = apply_candidate(candidate, _accepted_all())
    assert report.ok is False
    assert report.rolled_back is True
    # First file was applied, then rolled back from its snapshot.
    assert (work / "one.txt").read_text() == "one\n"
    assert (work / "two.txt").read_text() == "two — edited elsewhere\n"
    statuses = {e.path: e.status for e in report.entries}
    assert statuses["one.txt"] == "rolled-back"
    assert statuses["two.txt"] == "failed"


# -- inline diff rendering -------------------------------------------------

def test_render_diff_update_shows_plus_and_minus(tmp_path):
    from clip_harness.gate import render_diff
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.txt").write_text("one\ntwo\nthree\n")
    candidate = _stage(tmp_path, [FileSpec(
        path="a.txt", declared_op="update",
        content="one\nTWO\nthree\n")], work)
    diff = render_diff(candidate, candidate.entries[0])
    assert "-two" in diff and "+TWO" in diff
    # The presentation heads each diff with path + op tag, manifest first.
    text = candidate.presentation()
    assert "a.txt" in text and "UPDATE" in text
    assert text.index("+TWO") > text.index("a.txt")


def test_render_diff_create_is_full_addition_and_capped(tmp_path):
    from clip_harness.gate import MAX_DIFF_LINES, render_diff
    work = tmp_path / "work"
    work.mkdir()
    big = "".join(f"line {i}\n" for i in range(300))
    candidate = _stage(tmp_path, [FileSpec(
        path="big.txt", declared_op="create", content=big)], work)
    diff = render_diff(candidate, candidate.entries[0])
    assert "+line 0" in diff
    assert "-line" not in diff  # a create removes nothing
    assert "more lines — full diff in" in diff
    assert "line 299" not in diff  # capped, not dumped
    assert len(diff.splitlines()) <= MAX_DIFF_LINES + 1


def test_patch_without_trailing_newline_still_applies(tmp_path):
    # Regression: the payload parser strips the fence's trailing
    # newline, and git apply rejects unterminated patch lines
    # ("corrupt patch at line N") — staging must restore it.
    work = tmp_path / "work"
    work.mkdir()
    (work / "f.txt").write_text("a\nb\nc\n")
    patch = ("--- a/f.txt\n+++ b/f.txt\n@@ -1,3 +1,3 @@\n"
             " a\n-b\n+B\n c")  # no trailing newline, as parsed
    candidate = _stage(tmp_path, [FileSpec(
        path="f.txt", declared_op="auto", patch=patch)], work)
    entry = candidate.entries[0]
    staged = (candidate.dir / "candidates" / entry.candidate_file)
    assert staged.read_text() == "a\nB\nc\n"
