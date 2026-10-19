#!/usr/bin/env python3
"""Thin shim so the e2e matrix can drive the real CLI entry point
(clip_harness.cli:main) from a source checkout, without installing."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clip_harness.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
