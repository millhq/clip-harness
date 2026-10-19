"""Import hashlib once, quietly — swallow CPython's blake2 warning.

WHY: CPython's ``hashlib`` tries to register ``blake2b``/``blake2s``
at import time. On a Python built without them (no OpenSSL blake2
constructor and no builtin ``_blake2`` module — seen on corporate
pyenv builds), the registration fails and hashlib reports it with
``logging.exception(...)`` on the root logger:

    ERROR:root:code for hash blake2b was not found.
    Traceback (most recent call last):
      ...
    ValueError: unsupported hash type blake2b

— twice (blake2b, blake2s), on stderr, before the program prints
anything. clip-harness imports hashlib for sha256 work, so every
invocation on such a build opened with that spam even though nothing
is actually wrong (sha256 is unaffected).

THE FIX: the first hashlib import in the process happens here, with
logging temporarily disabled, so the import-time ``logging.exception``
goes nowhere. Logging is then restored to exactly the level the host
process had — we save ``logging.root.manager.disable`` and put that
value back, rather than assuming it was NOTSET. Nothing about
hashlib's behaviour changes; only its import moment is muffled.

This module is imported for its side effect by ``clip_harness``
(``__init__.py``), which every entry point imports first — the
console script, the ``hook`` dispatcher, the iTerm2 watch adapter —
so the guarded import always wins the race. :func:`ensure` is also
safe to call explicitly; it is a no-op once hashlib is loaded.
"""

from __future__ import annotations

import logging
import sys

__all__ = ["ensure"]


def ensure() -> None:
    """Import hashlib with logging briefly disabled (idempotent)."""
    if "hashlib" in sys.modules:
        return
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        import hashlib  # noqa: F401 — imported for the sys.modules cache
    finally:
        logging.disable(previous)


ensure()
