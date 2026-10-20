"""Shared fixtures: a runner wired to headless stand-ins only."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from clip_harness.adapters.headless import MemoryClipboard
from clip_harness.core import ExecResult
from clip_harness.runner import LifecycleRunner
from clip_harness.settings import Settings


class FakeExecutor:
    """Executor stand-in: records commands, returns canned results,
    and writes the output file exactly like the real executor."""

    def __init__(self, output: str = "ok\n", exit_code: int = 0):
        self.output = output
        self.exit_code = exit_code
        self.calls: list[str] = []
        self.kwargs: list[dict] = []  # per-call executor kwargs

    def run(self, command, cwd, timeout, cancel_event, output_path,
            **kwargs):
        self.calls.append(command)
        self.kwargs.append(kwargs)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(self.output, encoding="utf-8")
        return ExecResult(output=self.output,
                          exit_desc=str(self.exit_code),
                          exit_code=self.exit_code, elapsed=0.01,
                          output_file=str(output_path))


@pytest.fixture(autouse=True)
def _offline_update_feed(tmp_path, monkeypatch):
    """Keep every test offline-deterministic for the update check:
    the runner consults the feed through CLIP_HARNESS_UPDATE_BASE,
    pointed here at an empty file:// directory — the fetch fails
    fast, the verdict is None, and no [update] line can appear
    unless a test seeds the state dir's update cache itself."""
    feed = tmp_path / "update-feed"
    feed.mkdir()
    monkeypatch.setenv("CLIP_HARNESS_UPDATE_BASE", feed.as_uri())


@pytest.fixture
def clipboard():
    return MemoryClipboard()


@pytest.fixture
def make_runner(tmp_path, clipboard):
    def _make(*, settings: Settings | None = None, decider=None,
              executor: FakeExecutor | None = None, **kwargs):
        settings = settings or Settings(state_dir=tmp_path / "state")
        return LifecycleRunner(
            settings=settings, clipboard=clipboard, decider=decider,
            presenter=lambda _summary: None,
            executor=executor or FakeExecutor(), **kwargs)
    return _make


@pytest.fixture
def hook_script(tmp_path):
    """Write a hook handler script; returns its settings command."""
    def _write(name: str, source: str) -> str:
        path = tmp_path / name
        path.write_text(source, encoding="utf-8")
        return f"{sys.executable} {path}"
    return _write


def shell_payload(command: str, session: str | None = None,
                  action: str | None = None, extra_files: str = "") -> str:
    header = ""
    if session:
        header += f"session: {session}\n"
    if action:
        header += f"action: {action}\n"
    return f"```shell\n{header}{command}\n```{extra_files}"
