"""Regression tests for the blake2 import-noise guard (_quiet_hashlib).

On a Python built without blake2 (observed: a corporate pyenv 3.11.3),
importing hashlib makes CPython log "code for hash blake2b was not
found." plus a traceback, twice, to stderr. clip-harness imports
hashlib, so every CLI invocation opened with that spam.

These tests reproduce the broken build with a FAKE hashlib placed
first on PYTHONPATH: its import does exactly what CPython's hashlib
does on such a build (logging.exception inside an except block) and
then delegates everything to the real stdlib hashlib, so sha256 & co.
behave normally. A marker file proves the fake really was imported —
without it the "no spam" assertions could pass vacuously.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
SPAM = "code for hash blake2b was not found"

FAKE_HASHLIB = '''
    import importlib.util
    import logging
    import os

    _marker = os.environ.get("FAKE_HASHLIB_MARKER")
    if _marker:
        with open(_marker, "w", encoding="utf-8") as fh:
            fh.write("fake hashlib imported from " + __file__ + "\\n")

    # What CPython's hashlib does at import on a blake2-less build.
    for _name in ("blake2b", "blake2s"):
        try:
            raise ValueError("unsupported hash type " + _name)
        except ValueError:
            logging.exception("code for hash %s was not found.", _name)

    # Delegate to the real stdlib hashlib, loaded by path so this
    # fake (which shadows the name) is not what gets executed.
    _stdlib = os.path.dirname(os.__file__)
    _spec = importlib.util.spec_from_file_location(
        "_real_hashlib", os.path.join(_stdlib, "hashlib.py"))
    _real = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_real)


    def __getattr__(name):
        return getattr(_real, name)
'''


@pytest.fixture
def fake_hashlib(tmp_path):
    """A directory whose hashlib.py mimics a blake2-less build."""
    fake_dir = tmp_path / "fake_stdlib"
    fake_dir.mkdir()
    (fake_dir / "hashlib.py").write_text(
        textwrap.dedent(FAKE_HASHLIB), encoding="utf-8")
    return fake_dir, tmp_path / "marker.txt"


def run_python(fake, code_args, extra_env=None):
    fake_dir, marker = fake
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(fake_dir), str(SRC), env.get("PYTHONPATH", "")])
    env["FAKE_HASHLIB_MARKER"] = str(marker)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, *code_args],
        capture_output=True, text=True, env=env, timeout=60)
    return proc, marker


def test_fake_hashlib_spams_when_imported_unguarded(fake_hashlib):
    """Control: without the guard, the fake reproduces the defect —
    so the quiet-CLI test below cannot pass vacuously."""
    proc, marker = run_python(
        fake_hashlib, ["-c", "import hashlib;"
                             " print(hashlib.sha256(b'x').hexdigest())"])
    assert marker.exists()
    assert SPAM in proc.stderr
    assert "code for hash blake2s was not found" in proc.stderr
    assert proc.returncode == 0


def test_cli_version_is_quiet_with_broken_hashlib(fake_hashlib):
    """The real CLI entry point, with the broken-build hashlib in
    play: no import spam on stderr, the command still works."""
    proc, marker = run_python(
        fake_hashlib,
        ["-c", "import sys; from clip_harness.cli import main;"
               " sys.exit(main())", "version"])
    assert marker.exists()  # the fake hashlib really was imported
    assert SPAM not in proc.stderr
    assert "blake2s" not in proc.stderr
    assert proc.returncode == 0
    assert proc.stdout.startswith("clip-harness ")


def test_guard_restores_the_previous_logging_state(fake_hashlib):
    """The guard must restore logging exactly as it found it: a host
    that had WARNING+ disabled keeps that, ERROR still gets through,
    and no blake2 spam leaks during the guarded import."""
    script = textwrap.dedent("""
        import logging
        logging.disable(logging.WARNING)
        from clip_harness._quiet_hashlib import ensure
        ensure()
        logging.error("err-visible")
        logging.warning("warn-hidden")
        """)
    proc, marker = run_python(fake_hashlib, ["-c", script])
    assert marker.exists()
    assert proc.returncode == 0
    assert SPAM not in proc.stderr
    assert "err-visible" in proc.stderr
    assert "warn-hidden" not in proc.stderr
