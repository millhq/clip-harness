"""Unit tests: terminal diff color (render layer only), its gating,
the settings key, and the invariant that stored/sent text — the
clipboard, receipts, files — stays byte-for-byte plain even when
color is forced on."""

import json
import re

from clip_harness.render import (BOLD_CYAN, CYAN, DIM, GREEN, RED,
                                 RESET, colorize_diff, paint, printer,
                                 should_color)
from clip_harness.settings import load_settings

ESC = "\x1b["

DIFF = """\
    --- a/a.py
    +++ b/a.py
    @@ -1,2 +1,2 @@
     keep
    -old
    +new"""


# -- the colorizer --------------------------------------------------------

def test_colorize_line_classes():
    out = colorize_diff(DIFF).splitlines()
    assert out[0] == f"{BOLD_CYAN}    --- a/a.py{RESET}"
    assert out[1] == f"{BOLD_CYAN}    +++ b/a.py{RESET}"
    assert out[2] == f"{CYAN}    @@ -1,2 +1,2 @@{RESET}"
    assert out[3] == "     keep"                       # context: untouched
    assert out[4] == f"{RED}    -old{RESET}"
    assert out[5] == f"{GREEN}    +new{RESET}"


def test_colorize_headers_are_not_add_or_delete():
    # The +++ / --- headers must not fall into the +/- classes.
    out = colorize_diff(DIFF)
    assert f"{GREEN}    +++" not in out
    assert f"{RED}    ---" not in out


def test_colorize_unindented_diff():
    out = colorize_diff("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b")
    lines = out.splitlines()
    assert lines[0].startswith(BOLD_CYAN)
    assert lines[3] == f"{RED}-a{RESET}"
    assert lines[4] == f"{GREEN}+b{RESET}"


def test_colorize_no_diff_is_byte_identical():
    text = ("$ ls -la && cat f\n"
            "-rw-r--r-- 1 ali staff 5 a.py\n"
            "- a bullet\n"
            "+ a stray plus line\n"
            "---\n"
            "done\n")
    assert colorize_diff(text) == text
    assert colorize_diff("") == ""
    assert colorize_diff("no trailing newline") == "no trailing newline"


def test_colorize_header_lookalike_content_lines():
    # Inside a region, a removed "-- x" renders as "--- x" and an
    # added "+ x" as "+++ x": classified by their first character,
    # not mistaken for file headers.
    text = ("    --- a/x\n"
            "    +++ b/x\n"
            "    @@ -1,2 +1,2 @@\n"
            "    --- removed\n"
            "    +++ added\n"
            "    \\ No newline at end of file")
    out = colorize_diff(text).splitlines()
    assert out[3] == f"{RED}    --- removed{RESET}"
    assert out[4] == f"{GREEN}    +++ added{RESET}"
    assert out[5] == f"{DIM}    \\ No newline at end of file{RESET}"


def test_colorize_region_ends_at_non_diff_lines():
    text = ("files:\n"
            "  ✓ a.py — UPDATED\n"
            "    --- a/a.py\n"
            "    +++ b/a.py\n"
            "    @@ -1 +1 @@\n"
            "    -x\n"
            "    +y\n"
            "[goal] Ship it [active]\n"
            "[open tasks] - not a deletion")
    out = colorize_diff(text).splitlines()
    assert out[0] == "files:"
    assert out[1] == "  ✓ a.py — UPDATED"
    assert out[5] == f"{RED}    -x{RESET}"
    assert out[6] == f"{GREEN}    +y{RESET}"
    assert out[7] == "[goal] Ship it [active]"       # footer untouched
    assert out[8] == "[open tasks] - not a deletion"


def test_colorize_two_regions():
    text = ("    --- a/a\n    +++ b/a\n    @@ -1 +1 @@\n    -1\n    +2\n"
            "  b.py — UPDATE\n"
            "    --- a/b\n    +++ b/b\n    @@ -1 +1 @@\n    -3\n    +4")
    out = colorize_diff(text)
    assert out.count(BOLD_CYAN) == 4
    assert f"{GREEN}    +2{RESET}" in out
    assert f"{GREEN}    +4{RESET}" in out
    assert "  b.py — UPDATE" in out


# -- gating ---------------------------------------------------------------

class FakeStream:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


def test_should_color_matrix():
    env = {}
    assert should_color("auto", isatty=True, env=env) is True
    assert should_color("auto", isatty=False, env=env) is False
    assert should_color("always", isatty=False, env=env) is True
    assert should_color("always", isatty=True, env=env) is True
    assert should_color("never", isatty=True, env=env) is False
    assert should_color("never", isatty=False, env=env) is False
    assert should_color("bogus", isatty=True, env=env) is True  # → auto


def test_no_color_beats_everything():
    for env in ({"NO_COLOR": "1"}, {"NO_COLOR": ""}):
        assert should_color("always", isatty=True, env=env) is False
        assert should_color("auto", isatty=True, env=env) is False


def test_paint_applies_gating():
    tty, pipe = FakeStream(True), FakeStream(False)
    assert ESC in paint(DIFF, "auto", stream=tty, env={})
    assert paint(DIFF, "auto", stream=pipe, env={}) == DIFF
    assert ESC in paint(DIFF, "always", stream=pipe, env={})
    assert paint(DIFF, "never", stream=tty, env={}) == DIFF
    assert paint(DIFF, "always", stream=tty,
                 env={"NO_COLOR": "1"}) == DIFF


def test_printer_paints(capsys, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    printer("always")("--- a/x\n+++ b/x\n@@ -1 +1 @@\n+z")
    out = capsys.readouterr().out
    assert ESC in out and "+z" in out


# -- settings -------------------------------------------------------------

def test_settings_color_loads_and_overrides(tmp_path):
    user = tmp_path / "user.json"
    user.write_text(json.dumps({"color": "never"}), encoding="utf-8")
    assert load_settings(cwd=tmp_path, user_path=user).color == "never"
    project = tmp_path / ".clip-harness"
    project.mkdir()
    (project / "settings.json").write_text(
        json.dumps({"color": "always"}), encoding="utf-8")
    # Project scope overrides the user scalar.
    assert load_settings(cwd=tmp_path, user_path=user).color == "always"


def test_settings_color_invalid_falls_back_to_auto(tmp_path):
    user = tmp_path / "user.json"
    user.write_text(json.dumps({"color": "purple"}), encoding="utf-8")
    assert load_settings(cwd=tmp_path, user_path=user).color == "auto"
    missing = tmp_path / "nope.json"
    assert load_settings(cwd=tmp_path, user_path=missing).color == "auto"


# -- the invariant: color never reaches stored/sent text -------------------

def test_cli_color_always_clipboard_and_receipts_stay_plain(
        tmp_path, monkeypatch, capsys):
    """Drive the real CLI with color forced ON: the terminal copy of
    the result block is painted, while the clipboard text, the
    receipts ledger, and the applied file stay byte-for-byte plain."""
    from clip_harness.cli import main

    home = tmp_path / "home"
    config = home / ".config" / "clip-harness"
    config.mkdir(parents=True)
    state = tmp_path / "state"
    (config / "settings.json").write_text(json.dumps(
        {"color": "always", "state_dir": str(state)}), encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    clip = tmp_path / "clipboard.txt"
    clip.write_text(
        "```shell\nsession: s1\n```\n\n"
        "```file\npath: new.txt\nop: create\nhello\n```",
        encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLIP_HARNESS_CLIPBOARD_FILE", str(clip))
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.chdir(project)

    # Trigger: the mutation stages and parks (async default).
    assert main([]) == 0
    note = clip.read_text(encoding="utf-8")
    assert ESC not in note
    candidate_id = re.search(r"candidate (r-[0-9a-f]+)", note).group(1)

    # Decide + apply out-of-band: the result block is printed AND
    # lands on the clipboard.
    assert main(["candidate", "review", candidate_id,
                 "--decision", "accept"]) == 0
    printed = capsys.readouterr().out
    assert ESC in printed                     # terminal copy painted
    stripped = re.sub(r"\x1b\[[0-9;]*m", "", printed)
    assert "+hello" in stripped               # same words, painted

    block = clip.read_text(encoding="utf-8")
    assert block.startswith("```result")
    assert ESC not in block                   # clipboard stayed plain
    receipts = (state / "receipts.jsonl").read_text(encoding="utf-8")
    assert ESC not in receipts                # ledger stayed plain
    assert (project / "new.txt").read_text(encoding="utf-8") == "hello\n"
