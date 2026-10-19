"""Oversized-output elision: digest result blocks, preserved output.

The full output always survives byte-for-byte in runs/<run_id>/
output.txt; the result block carries a head + tail digest around one
elision marker once the output passes the `result_budget_chars`
setting (default 100k — room to spare inside a 128k-capped chat
message). The marker's counts are exact and it teaches retrieval
(sed/grep/tail on the stored file) at the moment it is needed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from clip_harness.core import (DEFAULT_RESULT_BUDGET_CHARS, Event,
                               elide_output, format_result)
from clip_harness.settings import Settings, load_settings

from conftest import FakeExecutor, shell_payload

MARKER = re.compile(
    r"\[output elided: (\d+) of (\d+) chars shown · "
    r"full output \((\d+) chars\) saved at (\S+) · "
    r"page it with: sed -n '(\d+),(\d+)p' \S+ · grep \S+ · "
    r"tail -c (\d+) \S+\]")


def numbered(lines: int) -> str:
    """Deterministic multi-line output, 21 chars per line."""
    return "".join(f"row {i:05d} " + "x" * 10 + "\n" for i in range(lines))


def marker_parts(text: str):
    match = MARKER.search(text)
    assert match, f"no elision marker in: {text[:200]!r}"
    shown, total, total2 = (int(match.group(i)) for i in (1, 2, 3))
    assert total == total2
    return match, shown, total


# -- elide_output (unit) -------------------------------------------------------


def test_under_allowance_is_untouched():
    result = elide_output("hello\n", 100, "/tmp/o.txt")
    assert not result.elided
    assert result.text == "hello\n"
    assert result.shown_chars == result.total_chars == 6


def test_exactly_at_allowance_is_untouched():
    result = elide_output("a" * 500, 500)
    assert not result.elided
    assert result.text == "a" * 500


def test_elision_counts_are_exact_and_bounded():
    output = numbered(1000)  # 21_000 chars
    result = elide_output(output, 2000, "/tmp/out.txt")
    assert result.elided
    assert len(result.text) <= 2000
    match, shown, total = marker_parts(result.text)
    assert total == 21_000 == result.total_chars
    assert shown == result.shown_chars
    assert int(match.group(7)) == total - shown  # tail -c <elided>
    head, tail = result.text.split("\n[output elided: ", 1)
    tail = tail.split("\n", 1)[1]  # drop the marker line itself
    # shown counts are literally the head + tail characters.
    assert len(head) + len(tail) == shown
    # Head is a prefix cut at a line boundary; tail is a suffix.
    assert output.startswith(head + "\n")
    assert output.endswith(tail)
    assert "row 00000" in head and "row 00999" in tail
    assert "row 00500" not in result.text  # the middle is really gone
    # The sed hint points into the elided span.
    first, last = int(match.group(5)), int(match.group(6))
    assert 1 < first <= last <= 1000


def test_single_line_longer_than_budget_is_bounded():
    result = elide_output("y" * 50_000, 1000, "/tmp/o.txt")
    assert result.elided
    assert len(result.text) <= 1000
    _match, shown, total = marker_parts(result.text)
    assert total == 50_000
    head, tail = result.text.split("\n[output elided: ", 1)
    tail = tail.split("\n", 1)[1]
    assert set(head) == {"y"} and set(tail) == {"y"}
    assert len(head) + len(tail) == shown


def test_pathological_allowance_returns_marker_only():
    result = elide_output(numbered(100), 10, "/tmp/o.txt")
    assert result.elided
    assert result.shown_chars == 0
    assert result.text.startswith("[output elided: 0 of 2100 chars shown")


def test_empty_output_never_elides():
    result = elide_output("", 0)
    assert not result.elided and result.text == ""


# -- format_result --------------------------------------------------------------


def test_format_result_elides_and_keeps_footer_last():
    block = format_result(
        "make big", numbered(1000), "0", 0.1, "/tmp/o.txt", budget=1000,
        standing_section="[goal] g [active]\n[open tasks] none")
    _match, _shown, total = marker_parts(block)
    assert total == 21_000
    assert len(block) <= 1000 + 200  # budget + echo/exit/footer slack
    assert block.index("[output elided:") < block.index("[goal]")
    assert block.rstrip("`").rstrip().endswith("[open tasks] none")


def test_format_result_small_output_passes_through():
    block = format_result("echo hi", "hi\n", "0", 0.0)
    assert "hi\n" in block and "elided" not in block
    assert format_result("true", "", "0", 0.0).count("(no output)") == 1


# -- runner level -----------------------------------------------------------------


def test_runner_elides_over_budget_and_preserves_full_output(
        make_runner, clipboard, tmp_path):
    output = numbered(1000)
    settings = Settings(state_dir=tmp_path / "state",
                        result_budget_chars=1000)
    runner = make_runner(settings=settings,
                         executor=FakeExecutor(output=output))
    outcome = runner.process_text(shell_payload("make big"), "/tmp")
    block = outcome.result_block
    _match, shown, total = marker_parts(block)
    assert total == len(output) and 0 < shown < 1000
    assert len(block) <= 1000 + 200
    # Single-artifact invariant: clipboard bytes == the returned block.
    assert clipboard.text == block
    # The stored artifact is the FULL output, byte-for-byte.
    artifact = runner.state_dir / "runs" / outcome.run_id / "output.txt"
    assert artifact.read_bytes() == output.encode()
    # Receipt carries the full size + the elision flag (additive data).
    post = [e for e in runner.ledger.entries(outcome.run_id)
            if e["event"] == Event.POST_RUN.value]
    assert post and post[0]["data"]["output_chars"] == len(output)
    assert post[0]["data"]["output_elided"] is True


def test_runner_under_budget_has_no_marker(make_runner, clipboard,
                                           tmp_path):
    settings = Settings(state_dir=tmp_path / "state",
                        result_budget_chars=1000)
    runner = make_runner(settings=settings,
                         executor=FakeExecutor(output="small\n"))
    outcome = runner.process_text(shell_payload("echo small"), "/tmp")
    assert "small" in outcome.result_block
    assert "elided" not in outcome.result_block
    assert clipboard.text == outcome.result_block
    post = [e for e in runner.ledger.entries(outcome.run_id)
            if e["event"] == Event.POST_RUN.value]
    assert post[0]["data"]["output_elided"] is False


def test_runner_budget_changes_the_cut_point(make_runner, tmp_path):
    output = numbered(1000)
    shown_by_budget = {}
    for budget in (1000, 5000):
        settings = Settings(state_dir=tmp_path / f"state-{budget}",
                            result_budget_chars=budget)
        runner = make_runner(settings=settings,
                             executor=FakeExecutor(output=output))
        outcome = runner.process_text(shell_payload("make big"), "/tmp")
        _m, shown, _t = marker_parts(outcome.result_block)
        shown_by_budget[budget] = shown
    assert shown_by_budget[1000] < shown_by_budget[5000] < len(output)


# -- settings parsing --------------------------------------------------------------


def _load(base: Path, user_data, project_data=None):
    base.mkdir(parents=True, exist_ok=True)
    user = base / "user.json"
    user.write_text(json.dumps(user_data))
    cwd = base / "proj"
    if project_data is not None:
        settings_dir = cwd / ".clip-harness"
        settings_dir.mkdir(parents=True)
        (settings_dir / "settings.json").write_text(json.dumps(project_data))
    return load_settings(cwd=cwd, user_path=user)


def test_settings_budget_parsing(tmp_path):
    default = DEFAULT_RESULT_BUDGET_CHARS
    assert _load(tmp_path / "a", {}).result_budget_chars == default
    assert _load(tmp_path / "b",
                 {"result_budget_chars": 5000}).result_budget_chars == 5000
    for bad in (0, -5, "abc", None, True, 12.5):
        loaded = _load(tmp_path / f"bad-{bad!r}",
                       {"result_budget_chars": bad})
        assert loaded.result_budget_chars == default, bad
    # Project scope overrides user scope, like every other scalar.
    loaded = _load(tmp_path / "c", {"result_budget_chars": 5000},
                   {"result_budget_chars": 2500})
    assert loaded.result_budget_chars == 2500
