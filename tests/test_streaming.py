"""Live run visibility: on interactive paths the terminal gets a run
header and the command's output as it is produced, with a heartbeat
during silence — presentation only. The captured output, the spill
file, the clipboard and the result block stay byte-for-byte what the
silent path produces; the watch/headless paths never stream.
"""

from __future__ import annotations

import io
import re
import sys
import threading
import time
import types
from pathlib import Path

from clip_harness import cli
from clip_harness.adapters.headless import MemoryClipboard
from clip_harness.executor import LocalExecutor
from clip_harness.runner import LifecycleRunner
from clip_harness.settings import Settings

from conftest import shell_payload


# -- executor: the streaming capture ------------------------------------

def _stream(tmp_path, command, chunks, timeout=10, cancel=None,
            heartbeat_interval=None):
    out = tmp_path / "out.txt"
    return LocalExecutor().run(
        command, str(tmp_path), timeout, cancel or threading.Event(),
        out, sink=chunks.append, heartbeat_interval=heartbeat_interval)


def test_streaming_chunks_arrive_progressively(tmp_path):
    chunks: list[str] = []
    result = _stream(tmp_path, "printf 'one\\n'; sleep 0.4; "
                               "printf 'two\\n'", chunks)
    assert result.exit_desc == "0"
    # The two prints were seen as SEPARATE sink calls, in order.
    one = [i for i, c in enumerate(chunks) if "one" in c]
    two = [i for i, c in enumerate(chunks) if "two" in c]
    assert one and two and one[0] < two[0] and one[0] != two[0]
    # Everything the terminal saw is exactly what was captured.
    assert "".join(chunks) == "one\ntwo\n" == result.output
    assert Path(result.output_file).read_text() == "one\ntwo\n"


def test_streaming_captures_stderr_and_exit_code(tmp_path):
    chunks: list[str] = []
    result = _stream(tmp_path, "echo oops >&2; exit 3", chunks)
    assert result.exit_code == 3 and result.failed is True
    assert "oops" in result.output and "oops" in "".join(chunks)


def test_streaming_spill_file_written_incrementally(tmp_path):
    """Mid-run, the spill file already holds what has been produced
    (tail -f works) — observed from the sink itself, which fires
    after each chunk is written and flushed."""
    out = tmp_path / "out.txt"
    seen_mid_run: list[str] = []

    def sink(_text):
        seen_mid_run.append(out.read_text() if out.exists() else "")

    LocalExecutor().run("printf 'early\\n'; sleep 0.4; printf 'late\\n'",
                        str(tmp_path), 10, threading.Event(), out,
                        sink=sink)
    assert any("early" in snap for snap in seen_mid_run)
    assert out.read_text() == "early\nlate\n"


def test_heartbeat_fires_in_silence_and_is_never_captured(tmp_path):
    chunks: list[str] = []
    result = _stream(tmp_path, "sleep 1.0", chunks,
                     heartbeat_interval=0.2)
    assert result.exit_desc == "0"
    assert any("… running" in c for c in chunks)
    assert result.output == ""
    assert Path(result.output_file).read_text() == ""


def test_heartbeat_waits_for_a_full_silent_interval(tmp_path):
    chunks: list[str] = []
    _stream(tmp_path, "printf 'x\\n'", chunks, heartbeat_interval=5)
    assert not any("… running" in c for c in chunks)


def test_streaming_timeout_still_bounds_the_run(tmp_path):
    chunks: list[str] = []
    started = time.monotonic()
    result = _stream(tmp_path, "sleep 10", chunks, timeout=1)
    assert result.timed_out is True and result.exit_desc == "timeout"
    assert time.monotonic() - started < 5


def test_streaming_cancel_event_still_interrupts(tmp_path):
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    result = _stream(tmp_path, "sleep 10", [], cancel=cancel)
    assert result.cancelled is True and result.exit_desc == "cancelled"


def test_failing_sink_never_wedges_the_run(tmp_path):
    def bad_sink(_text):
        raise RuntimeError("terminal went away")

    out = tmp_path / "out.txt"
    result = LocalExecutor().run("printf 'a\\n'; printf 'b\\n'",
                                 str(tmp_path), 10, threading.Event(),
                                 out, sink=bad_sink)
    assert result.exit_desc == "0" and result.output == "a\nb\n"


def test_unstreamed_path_unchanged(tmp_path):
    out = tmp_path / "out.txt"
    result = LocalExecutor().run("printf 'plain\\n'", str(tmp_path), 10,
                                 threading.Event(), out)
    assert result.output == "plain\n"
    assert out.read_text() == "plain\n"


# -- runner: header, streaming, and the single-artifact invariant -------

def _runner(tmp_path, *, stream: bool, sink=None, presenter=None,
            heartbeat_interval=None):
    return LifecycleRunner(
        settings=Settings(state_dir=tmp_path / "state"),
        clipboard=MemoryClipboard(),
        presenter=presenter or (lambda _s: None),
        executor=LocalExecutor(),
        stream_output=stream, stream_sink=sink,
        heartbeat_interval=heartbeat_interval)


def test_header_presented_before_first_output(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    log: list[tuple[str, str]] = []
    runner = _runner(
        tmp_path, stream=True,
        presenter=lambda text: log.append(("header", text)),
        sink=lambda text: log.append(("sink", text)))
    outcome = runner.process_text(
        shell_payload("printf 'one\\n'; sleep 0.3; printf 'two\\n'",
                      session="live-vis"), str(work))
    kinds = [kind for kind, _ in log]
    assert kinds[0] == "header" and "sink" in kinds
    header = log[0][1]
    assert header.startswith("$ printf 'one")
    assert f"cwd: {work}" in header
    assert "session: live-vis" in header
    assert "timeout:" in header
    streamed = "".join(text for kind, text in log if kind == "sink")
    assert streamed == "one\ntwo\n"
    # None of the live view leaks into the artifact: the header's
    # meta line appears nowhere in the block, and the command is
    # echoed exactly once (the block's own `$ ` line).
    assert outcome.result_block is not None
    assert f"cwd: {work}" not in outcome.result_block
    assert "timeout:" not in outcome.result_block.split("$ ", 1)[0]
    assert outcome.result_block.count("$ printf") == 1


def test_streamed_and_unstreamed_result_blocks_identical(tmp_path):
    command = "printf 'alpha\\n'; sleep 0.3; printf 'beta\\n'; exit 0"
    blocks, spills = [], []
    for name, stream in (("plain", False), ("live", True)):
        work = tmp_path / name
        work.mkdir()
        chunks: list[str] = []
        runner = _runner(tmp_path / name, stream=stream,
                         sink=chunks.append)
        outcome = runner.process_text(
            shell_payload(command, session="same"), str(work))
        blocks.append(re.sub(r"· [0-9.]+s", "· Ts",
                             outcome.result_block))
        spills.append(Path(runner.state_dir, "runs", outcome.run_id,
                           "output.txt").read_text())
        if stream:
            assert "".join(chunks) == "alpha\nbeta\n"
    assert blocks[0] == blocks[1]  # modulo the elapsed-time field
    assert spills[0] == spills[1] == "alpha\nbeta\n"


def test_heartbeat_absent_from_result_block(tmp_path):
    chunks: list[str] = []
    runner = _runner(tmp_path, stream=True, sink=chunks.append,
                     heartbeat_interval=0.2)
    outcome = runner.process_text(shell_payload("sleep 0.8"),
                                  str(tmp_path))
    assert any("… running" in c for c in chunks)
    assert "… running" not in outcome.result_block
    assert "(no output)" in outcome.result_block


def test_unstreamed_runner_presents_no_header(tmp_path):
    presented: list[str] = []
    runner = _runner(tmp_path, stream=False,
                     presenter=presented.append)
    runner.process_text(shell_payload("printf 'x\\n'"), str(tmp_path))
    assert presented == []


# -- CLI wiring -----------------------------------------------------------

class _TtyStdout(io.StringIO):
    def isatty(self):
        return True


def _capture_cli_runner(monkeypatch, tmp_path):
    captured: dict = {}

    class _StubRunner:
        def process_text(self, text, cwd, allow_bare=False):
            return None

    def factory(**kwargs):
        captured.update(kwargs)
        return _StubRunner()

    monkeypatch.setattr(cli, "LifecycleRunner", factory)
    monkeypatch.setattr(
        cli, "load_settings",
        lambda: Settings(state_dir=tmp_path / "state"))
    monkeypatch.setattr(
        cli, "_default_clipboard",
        lambda: MemoryClipboard("```shell\necho hi\n```"))
    return captured


def test_cli_trigger_streams_at_a_tty(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "stdout", _TtyStdout())
    captured = _capture_cli_runner(monkeypatch, tmp_path)
    assert cli.cmd_trigger(types.SimpleNamespace()) == 0
    assert captured.get("stream_output") is True
    assert callable(captured.get("stream_sink"))


def test_cli_run_streams_at_a_tty(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "stdout", _TtyStdout())
    captured = _capture_cli_runner(monkeypatch, tmp_path)
    args = cli.build_parser().parse_args(["run", "--command", "echo hi"])
    cli.cmd_run(args)
    assert captured.get("stream_output") is True
    assert callable(captured.get("stream_sink"))


def test_cli_trigger_does_not_stream_without_a_tty(monkeypatch,
                                                  tmp_path):
    monkeypatch.setattr(sys, "stdout", io.StringIO())  # isatty() False
    captured = _capture_cli_runner(monkeypatch, tmp_path)
    assert cli.cmd_trigger(types.SimpleNamespace()) == 0
    assert not captured.get("stream_output")


def test_cli_watch_never_streams_even_at_a_tty(monkeypatch, tmp_path):
    import clip_harness.adapters.watch_local as watch_local

    class _StubWatch:
        def __init__(self, runner, clipboard, cwd):
            pass

        def run_forever(self):
            return None

    monkeypatch.setattr(watch_local, "WatchLoop", _StubWatch)
    monkeypatch.setattr(sys, "stdout", _TtyStdout())
    captured = _capture_cli_runner(monkeypatch, tmp_path)
    args = cli.build_parser().parse_args(["watch"])
    assert cli.cmd_watch(args) == 0
    assert not captured.get("stream_output")
