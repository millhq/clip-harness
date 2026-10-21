# Troubleshooting

**Every command opens with `ERROR:root: code for hash blake2b was not
found.` and a traceback.** Your Python was built without blake2
support (no OpenSSL blake2 constructor, no builtin `_blake2` module —
seen on corporate pyenv builds). CPython's `hashlib` logs that
traceback itself at import time; clip-harness imports hashlib for
sha256, so the spam lands on stderr before anything runs. Nothing is
broken — sha256 is unaffected. clip-harness silences it by making
its first hashlib import with logging briefly disabled (see
`clip_harness/_quiet_hashlib.py`). The permanent fix is a Python
built against a full OpenSSL.
