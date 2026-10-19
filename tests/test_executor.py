"""LocalExecutor against the real shell (fast commands only)."""

import threading
import time
from pathlib import Path

from clip_harness.executor import LocalExecutor


def _run(tmp_path, command, timeout=10, cancel=None):
    out = tmp_path / "out.txt"
    return LocalExecutor().run(command, str(tmp_path), timeout,
                               cancel or threading.Event(), out)


def test_echo_and_exit_zero(tmp_path):
    result = _run(tmp_path, "echo hello")
    assert result.exit_desc == "0"
    assert result.output == "hello\n"
    assert result.failed is False
    assert Path(result.output_file).read_text() == "hello\n"


def test_nonzero_exit(tmp_path):
    result = _run(tmp_path, "echo out; exit 3")
    assert result.exit_desc == "3"
    assert result.exit_code == 3
    assert result.failed is True


def test_stderr_merges_into_output(tmp_path):
    result = _run(tmp_path, "echo oops >&2")
    assert "oops" in result.output


def test_timeout(tmp_path):
    started = time.monotonic()
    result = _run(tmp_path, "sleep 10", timeout=1)
    assert result.timed_out is True
    assert result.exit_desc == "timeout"
    assert time.monotonic() - started < 5


def test_cancel_event(tmp_path):
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    result = _run(tmp_path, "sleep 10", cancel=cancel)
    assert result.cancelled is True
    assert result.exit_desc == "cancelled"
