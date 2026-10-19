"""Parser tests: the fence ladder, headers, file/patch fences, triggers."""

import pytest

from clip_harness.payload import (PayloadError, parse_clipboard_text,
                                  parse_payload)


def test_shell_fence_is_canonical():
    payload = parse_payload("```shell\necho hi\n```")
    assert payload.command == "echo hi"
    assert payload.session_id is None
    assert payload.bound is False


def test_run_fence_is_legacy_alias():
    payload = parse_payload("```run\necho hi\n```")
    assert payload.command == "echo hi"


def test_metadata_header_session_binds():
    payload = parse_payload(
        "```shell\nsession: auth-1\naction: run tests\ntimeout: 30\n"
        "npm test\n```")
    assert payload.session_id == "auth-1"
    assert payload.bound is True
    assert payload.action == "run tests"
    assert payload.timeout == 30.0
    assert payload.command == "npm test"


def test_header_cwd_and_timeout():
    payload = parse_payload("```shell\ncwd: /tmp/x\ntimeout: 5\npwd\n```")
    assert payload.cwd == "/tmp/x"
    assert payload.timeout == 5.0


def test_bad_timeout_is_malformed():
    with pytest.raises(PayloadError):
        parse_payload("```shell\ntimeout: soon\npwd\n```")


def test_unclosed_fence_is_malformed():
    with pytest.raises(PayloadError):
        parse_payload("```shell\necho hi\n")


def test_two_shell_fences_is_malformed():
    with pytest.raises(PayloadError):
        parse_payload("```shell\necho a\n```\n```shell\necho b\n```")


def test_empty_command_is_malformed():
    with pytest.raises(PayloadError):
        parse_payload("```shell\nsession: x\n```")


def test_plain_text_is_not_a_payload():
    assert parse_payload("just some copied text") is None
    assert parse_payload("echo hi") is None


def test_file_fence_full_content():
    text = ("```file\npath: src/a.py\nop: create\n"
            "print('a')\nprint('b')\n```")
    payload = parse_payload(text)
    assert payload.pure_mutation is True
    assert len(payload.files) == 1
    spec = payload.files[0]
    assert spec.path == "src/a.py"
    assert spec.declared_op == "create"
    assert spec.content == "print('a')\nprint('b')\n"
    assert spec.is_patch is False


def test_file_fence_default_op_auto():
    payload = parse_payload("```file\npath: x.txt\nhello\n```")
    assert payload.files[0].declared_op == "auto"


def test_patch_fence():
    text = ("```patch\npath: src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
            "@@ -1 +1 @@\n-old\n+new\n```")
    payload = parse_payload(text)
    spec = payload.files[0]
    assert spec.is_patch is True
    assert "-old" in spec.patch


def test_file_fence_requires_path_line():
    with pytest.raises(PayloadError):
        parse_payload("```file\nop: create\nhello\n```")


def test_mixed_shell_and_file_fences():
    text = ("```shell\nsession: s1\nmake test\n```\n"
            "```file\npath: a.txt\nhello\n```")
    payload = parse_payload(text)
    assert payload.command == "make test"
    assert payload.session_id == "s1"
    assert len(payload.files) == 1
    assert payload.pure_mutation is False


def test_cancel_trigger_plain_and_with_id():
    payload = parse_payload("```cancel```")
    assert payload.kind == "cancel" and payload.session_id is None
    payload = parse_payload("```cancel id=auth-1```")
    assert payload.kind == "cancel" and payload.session_id == "auth-1"


def test_rerun_trigger():
    payload = parse_payload("```rerun```")
    assert payload.kind == "rerun"


def test_cancelled_is_not_a_trigger():
    assert parse_payload("```cancelled```") is None


# -- the bare-text rung of the ladder ------------------------------------

def test_bare_text_runs_only_when_allowed():
    payload = parse_clipboard_text('echo "hello world"', allow_bare=True)
    assert payload.command == 'echo "hello world"'
    assert payload.session_id is None
    assert parse_clipboard_text('echo "hello world"',
                                allow_bare=False) is None


def test_bare_never_reruns_own_output():
    assert parse_clipboard_text("```result\n$ x\nok\n```",
                                allow_bare=True) is None
    assert parse_clipboard_text("⛔ clip-harness: run denied — nope",
                                allow_bare=True) is None
    assert parse_clipboard_text("⏳ clip-harness: candidate staged",
                                allow_bare=True) is None


def test_bare_empty_is_none():
    assert parse_clipboard_text("   \n ", allow_bare=True) is None
