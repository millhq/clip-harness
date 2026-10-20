"""clip-harness — a human-in-the-loop clipboard execution bridge.

The project name lives here (and in pyproject.toml) so it can be renamed
in one obvious place before publishing.
"""

# First statement on purpose: importing any clip_harness module runs
# this package first, so the guarded hashlib import in _quiet_hashlib
# happens before anything else in the process can pull hashlib in
# noisily (see that module for the blake2 story).
from . import _quiet_hashlib  # noqa: F401

NAME = "clip-harness"
__version__ = "0.2.0"

__all__ = ["NAME", "__version__"]
