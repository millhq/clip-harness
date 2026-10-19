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


def test_attend_header_opts_into_inline_review():
    payload = parse_payload("```shell\nattend: true\necho hi\n```")
    assert payload.attend is True
    assert payload.command == "echo hi"
    plain = parse_payload("```shell\necho hi\n```")
    assert plain.attend is False


# -- the unfenced form (fence-stripped copies, e.g. M365 Copilot) --------
#
# Some chat UIs copy a code block's contents without the outer fence.
# Text that BEGINS with a metadata header parses as that shell payload;
# anything else keeps today's behavior exactly.

ACCEPTANCE_UNFENCED = (
    "session: clip-harness-smoke\n"
    "timeout: 30\n"
    "printf 'clip_harness_smoke=ok\\n'")


def test_unfenced_acceptance_example():
    payload = parse_clipboard_text(ACCEPTANCE_UNFENCED, allow_bare=True)
    assert payload is not None
    assert payload.session_id == "clip-harness-smoke"
    assert payload.bound is True
    assert payload.timeout == 30.0
    # The executed command is exactly the body — header lines never
    # reach the shell.
    assert payload.command == "printf 'clip_harness_smoke=ok\\n'"


def test_unfenced_is_a_payload_form_not_bare_text():
    # Recognised on the watch path too (allow_bare=False): a metadata
    # header is a deliberate protocol act, like a fence.
    payload = parse_clipboard_text(ACCEPTANCE_UNFENCED, allow_bare=False)
    assert payload is not None
    assert payload.command == "printf 'clip_harness_smoke=ok\\n'"


def test_unfenced_full_header():
    payload = parse_clipboard_text(
        "session: s1\ncwd: /tmp/x\nattend: true\necho hi",
        allow_bare=True)
    assert payload.session_id == "s1"
    assert payload.cwd == "/tmp/x"
    assert payload.attend is True
    assert payload.command == "echo hi"


def test_unfenced_goal_set_action():
    # The exact shape observed failing in M365: an action payload
    # whose fence the copy button stripped.
    payload = parse_clipboard_text(
        "session: velocity-navigator\n"
        "action: goal-set\n"
        "Build the offline, discovery-first Velocity Navigator "
        "foundation without pivoting.",
        allow_bare=True)
    assert payload.session_id == "velocity-navigator"
    assert payload.action == "goal-set"
    assert payload.command == (
        "Build the offline, discovery-first Velocity Navigator "
        "foundation without pivoting.")


def test_unfenced_task_add_action():
    payload = parse_clipboard_text(
        "session: s1\naction: task-add\nFirst task\nSecond task",
        allow_bare=True)
    assert payload.action == "task-add"
    assert payload.command == "First task\nSecond task"


def test_unfenced_unknown_first_line_stays_bare():
    # `foo:` is not a recognised key — the whole text is a bare
    # command, exactly as before this form existed.
    payload = parse_clipboard_text("foo: bar\necho hi", allow_bare=True)
    assert payload.session_id is None
    assert payload.command == "foo: bar\necho hi"
    assert parse_clipboard_text("foo: bar\necho hi",
                                allow_bare=False) is None


def test_unfenced_bad_timeout_fails_closed():
    with pytest.raises(PayloadError):
        parse_clipboard_text("session: s1\ntimeout: soon\necho hi",
                             allow_bare=True)
    with pytest.raises(PayloadError):
        parse_clipboard_text("timeout: soon\necho hi", allow_bare=False)


def test_unfenced_bad_attend_fails_closed():
    with pytest.raises(PayloadError):
        parse_clipboard_text("attend: maybe\necho hi", allow_bare=True)


def test_unfenced_metadata_only_is_malformed():
    with pytest.raises(PayloadError):
        parse_clipboard_text("session: s1\ntimeout: 30", allow_bare=True)
    with pytest.raises(PayloadError):
        parse_clipboard_text("session: s1\naction: goal-set",
                             allow_bare=True)


def test_unfenced_metadata_after_body_is_shell_text():
    # Once the body begins, a `session:` line is ordinary shell text.
    payload = parse_clipboard_text(
        "session: s1\necho one\nsession: not-a-header\necho two",
        allow_bare=True)
    assert payload.session_id == "s1"
    assert payload.command == "echo one\nsession: not-a-header\necho two"


def test_unfenced_header_line_alone_is_not_bare():
    # A metadata-only paste must fail closed even on the trigger path —
    # never fall through to executing the header as a bare command.
    with pytest.raises(PayloadError):
        parse_clipboard_text("session: s1", allow_bare=True)


def test_unfenced_parse_payload_alone_still_none():
    # parse_payload is the fenced ladder only; the unfenced rung
    # lives in parse_clipboard_text.
    assert parse_payload(ACCEPTANCE_UNFENCED) is None


def test_fenced_form_unchanged_by_unfenced_rung():
    payload = parse_clipboard_text(
        "```shell\nsession: s1\ntimeout: 30\necho fenced\n```",
        allow_bare=True)
    assert payload.session_id == "s1"
    assert payload.timeout == 30.0
    assert payload.command == "echo fenced"


# -- the unfenced mutation form (fence-stripped ```file / ```patch) -----
#
# The same copy buttons strip the mutation fences too. The contents
# parse as the same FileSpec the fenced parser would build — under
# stricter recognition rules, and every ambiguity fails closed.

UNFENCED_FILE_CREATE = "path: src/new.py\nop: create\nprint('new')\n"
UNFENCED_PATCH = ("path: src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
                  "@@ -1 +1 @@\n-old\n+new\n")


def test_unfenced_file_create():
    payload = parse_clipboard_text(UNFENCED_FILE_CREATE, allow_bare=True)
    assert payload.pure_mutation is True
    assert payload.command == ""
    spec = payload.files[0]
    assert spec.path == "src/new.py"
    assert spec.declared_op == "create"
    assert spec.content == "print('new')\n"
    assert spec.is_patch is False


def test_unfenced_file_is_a_payload_form_not_bare_text():
    # Recognised on the watch path too (allow_bare=False).
    payload = parse_clipboard_text(UNFENCED_FILE_CREATE, allow_bare=False)
    assert payload is not None and payload.pure_mutation is True


def test_unfenced_file_update_and_delete():
    payload = parse_clipboard_text(
        "path: a.txt\nop: update\nv2\n", allow_bare=True)
    assert payload.files[0].declared_op == "update"
    assert payload.files[0].content == "v2\n"
    payload = parse_clipboard_text(
        "path: a.txt\nop: delete\n", allow_bare=True)
    assert payload.files[0].declared_op == "delete"
    assert payload.files[0].content == ""


def test_unfenced_patch():
    payload = parse_clipboard_text(UNFENCED_PATCH, allow_bare=True)
    spec = payload.files[0]
    assert spec.is_patch is True
    assert spec.path == "src/a.py"
    assert "-old" in spec.patch and "+new" in spec.patch


def test_unfenced_file_missing_op_fails_closed():
    # Plain content after `path:` with no op: line is NOT a file
    # payload — and must never degrade into a bare shell command.
    with pytest.raises(PayloadError):
        parse_clipboard_text("path: notes.txt\njust some prose\n",
                             allow_bare=True)
    with pytest.raises(PayloadError):
        parse_clipboard_text("path: notes.txt\njust some prose\n",
                             allow_bare=False)


def test_unfenced_file_invalid_op_fails_closed():
    with pytest.raises(PayloadError):
        parse_clipboard_text("path: a.txt\nop: frobnicate\nbody\n",
                             allow_bare=True)


def test_unfenced_op_auto_fails_closed():
    # Stricter than the fence: in the unfenced form the op must be
    # explicit — `auto` is fenced-only.
    with pytest.raises(PayloadError):
        parse_clipboard_text("path: a.txt\nop: auto\nbody\n",
                             allow_bare=True)
    # The fenced grammar is unchanged: auto still parses there.
    payload = parse_payload("```file\npath: a.txt\nop: auto\nbody\n```")
    assert payload.files[0].declared_op == "auto"


def test_unfenced_delete_with_body_fails_closed():
    with pytest.raises(PayloadError):
        parse_clipboard_text("path: a.txt\nop: delete\nleftover\n",
                             allow_bare=True)
    # Fenced delete tolerates (ignores) a body, as before.
    payload = parse_payload(
        "```file\npath: a.txt\nop: delete\nignored\n```")
    assert payload.files[0].declared_op == "delete"
    assert payload.files[0].content == ""


def test_unfenced_create_update_empty_body_fails_closed():
    with pytest.raises(PayloadError):
        parse_clipboard_text("path: a.txt\nop: create\n",
                             allow_bare=True)
    with pytest.raises(PayloadError):
        parse_clipboard_text("path: a.txt\nop: update\n  \n",
                             allow_bare=True)
    with pytest.raises(PayloadError):
        parse_clipboard_text("path: a.txt\nop: create",
                             allow_bare=True)


def test_unfenced_invalid_diff_header_fails_closed():
    # `path:` then a body that is neither op:-headed nor a real
    # unified diff (second line is not `+++`).
    with pytest.raises(PayloadError):
        parse_clipboard_text(
            "path: a.py\n--- a/a.py\n-- not a plus header\n-old\n",
            allow_bare=True)
    # `path:` alone, nothing after it.
    with pytest.raises(PayloadError):
        parse_clipboard_text("path: a.py", allow_bare=True)


def test_unfenced_op_wins_over_diff_body():
    # Precedence: an explicit op: line means file.write, even when
    # the content is itself a diff.
    payload = parse_clipboard_text(
        "path: change.patch\nop: create\n"
        "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
        allow_bare=True)
    spec = payload.files[0]
    assert spec.is_patch is False
    assert spec.declared_op == "create"
    assert spec.content.startswith("--- a/x")


def test_unfenced_file_content_is_verbatim():
    text = ("path: doc.md\nop: create\n# Title\n\n"
            "path: not a header inside content\n- a list\n")
    payload = parse_clipboard_text(text, allow_bare=True)
    assert payload.files[0].content == (
        "# Title\n\npath: not a header inside content\n- a list\n")


def test_path_line_after_shell_body_is_shell_text():
    # `path:` appearing after a shell body has begun is ordinary
    # shell text, never a file payload.
    payload = parse_clipboard_text(
        "session: s1\necho one\npath: not-a-file\necho two",
        allow_bare=True)
    assert payload.command == "echo one\npath: not-a-file\necho two"
    assert payload.files == []
    fenced = parse_payload(
        "```shell\necho one\npath: not-a-file\n```")
    assert fenced.command == "echo one\npath: not-a-file"
    assert fenced.files == []
