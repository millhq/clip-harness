"""Self-update: notify in-band, upgrade on one explicit command.

The pattern is the converged dev-tool one (gh, npm, rustup): the
harness NEVER replaces itself silently — a tool whose job is
reviewing what executes does not change underneath a session. A
throttled check (at most one fetch per day, cached in
``<state_dir>/update-check.json``) learns the latest release from
the release feed; when a newer version exists, one ``[update]``
line rides the result blocks — visible to the human, and to the
model that reads the block back — naming the exact upgrade command.
``clip-harness upgrade`` performs the swap: the wheel is downloaded
and verified against the release's SHA256SUMS, failing closed on
any mismatch, and only then installed.

The feed is GitHub Releases' stable latest-download URLs — no API
token, no extra infrastructure: ``/releases/latest/download/<asset>``
redirects to the newest published release. The
``CLIP_HARNESS_UPDATE_BASE`` environment variable replaces that base
(mirror support; the tests use it with a ``file://`` directory feed).

Stdlib only, like the rest of the harness.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

DEFAULT_FEED_BASE = ("https://github.com/millhq/clip-harness"
                     "/releases/latest/download")
FEED_ENV_VAR = "CLIP_HARNESS_UPDATE_BASE"
CACHE_NAME = "update-check.json"
CHECK_INTERVAL_S = 24 * 3600
FETCH_TIMEOUT_S = 2

_WHEEL_RE = re.compile(r"^clip_harness-(.+)-py3-none-any\.whl$")


class UpdateError(Exception):
    """A user-facing upgrade failure: fail closed, nothing installed."""


def feed_base() -> str:
    return os.environ.get(FEED_ENV_VAR, DEFAULT_FEED_BASE).rstrip("/")


# ----------------------------------------------------------------------
# Versions and feed parsing
# ----------------------------------------------------------------------

def parse_version(text: str) -> tuple[int, ...]:
    """``"v0.2"`` -> ``(0, 2, 0)``: a leading ``v`` is tolerated and
    missing components count as 0. Non-numeric components count as 0
    rather than raising — version strings here are ours and clean,
    and the check path must never raise into a run."""
    parts = text.strip().lstrip("v").split(".")
    nums = [int(part) if part.isdigit() else 0 for part in parts]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def parse_checksums(data: bytes) -> dict[str, str]:
    """SHA256SUMS bytes -> ``{filename: sha256}``. Lines are the
    coreutils form ``<hex>  <name>``; a ``*`` binary marker on the
    name is tolerated, malformed lines are skipped."""
    out: dict[str, str] = {}
    for line in data.decode("utf-8", errors="replace").splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        digest, name = fields
        out[name.lstrip("*")] = digest.lower()
    return out


def wheel_version(filename: str) -> str | None:
    """The version named by a wheel asset filename, else None."""
    match = _WHEEL_RE.match(filename)
    return match.group(1) if match else None


def _wheel_name(sums: dict[str, str]) -> str | None:
    return next((name for name in sums if wheel_version(name)), None)


# ----------------------------------------------------------------------
# Fetching (seam: a callable(url) -> bytes)
# ----------------------------------------------------------------------

def _fetch(url: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "clip-harness"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_S) \
            as response:
        return response.read()


def _fetch_latest(fetcher) -> str | None:
    """The feed's latest version, or None on ANY failure."""
    try:
        sums = parse_checksums(fetcher(f"{feed_base()}/SHA256SUMS"))
    except Exception:  # noqa: BLE001 — the check must never raise
        return None
    name = _wheel_name(sums)
    return wheel_version(name) if name else None


# ----------------------------------------------------------------------
# The throttled check
# ----------------------------------------------------------------------

def _read_cache(state_dir) -> dict | None:
    try:
        data = json.loads((Path(state_dir) / CACHE_NAME)
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_cache(state_dir, latest: str | None, now: float) -> None:
    try:
        path = Path(state_dir) / CACHE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"checked_at": now,
                                    "latest": latest}),
                        encoding="utf-8")
    except OSError:
        pass  # a read-only state dir loses the throttle, never the run


def check_for_update(state_dir, current_version: str, *,
                     now: float | None = None, fetcher=None,
                     force: bool = False) -> str | None:
    """The latest version if it is newer than ``current_version``,
    else None. ``force`` bypasses the daily throttle (used by
    ``version --check``).

    NEVER raises: offline, a slow feed, a corrupt cache — every
    failure resolves to the cached verdict, or None. A failed fetch
    slides the cache timestamp forward (keeping the cached verdict),
    so an unreachable feed is retried at most once a day rather than
    on every run."""
    now = time.time() if now is None else now
    cache = _read_cache(state_dir)
    cached_latest = cache.get("latest") if cache else None
    if not force and cache is not None:
        try:
            fresh = now - float(cache["checked_at"]) < CHECK_INTERVAL_S
        except (KeyError, TypeError, ValueError):
            fresh = False
        if fresh:
            return cached_latest if cached_latest and \
                is_newer(cached_latest, current_version) else None
    latest = _fetch_latest(fetcher or _fetch)
    if latest is not None:
        _write_cache(state_dir, latest, now)
    elif not force:
        _write_cache(state_dir, cached_latest, now)
        latest = cached_latest
    else:
        latest = cached_latest
    return latest if latest and is_newer(latest, current_version) \
        else None


def notice_line(latest: str, current: str) -> str:
    """The one line that rides a result block (immediately above the
    sticky footer) when an update is available."""
    return (f"[update] clip-harness {latest} is available "
            f"(you run {current}) — upgrade with: clip-harness upgrade")


# ----------------------------------------------------------------------
# The upgrade command
# ----------------------------------------------------------------------

def _default_installer(wheel_path: Path) -> None:
    """Install the verified wheel into the CURRENT environment. The
    harness runs inside its own pipx venv, so installing with the
    current interpreter swaps the package in place — and pip
    regenerates BOTH entry points (``clip-harness`` and ``clip``)."""
    import importlib.util
    if importlib.util.find_spec("pip") is None:
        raise UpdateError("pip is not available in this environment")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--force-reinstall",
         "--no-deps", str(wheel_path)])
    if proc.returncode != 0:
        raise UpdateError(
            f"pip install failed (exit {proc.returncode})")


def upgrade(current_version: str, *, fetcher=None, installer=None,
            out=print) -> int:
    """Download the latest release wheel, verify it against the
    feed's SHA256SUMS, and install it. Exit code semantics: 0 =
    upgraded or already latest; non-zero = nothing was installed."""
    fetch = fetcher or _fetch
    base = feed_base()
    try:
        sums = parse_checksums(fetch(f"{base}/SHA256SUMS"))
    except Exception as exc:  # noqa: BLE001 — reported, not raised
        out(f"clip-harness upgrade: could not read the release "
            f"feed: {exc}")
        return 1
    name = _wheel_name(sums)
    if name is None:
        out("clip-harness upgrade: the release feed names no wheel "
            "in SHA256SUMS — aborting, nothing installed")
        return 1
    latest = wheel_version(name)
    if not is_newer(latest, current_version):
        out(f"clip-harness {current_version} is already the "
            f"latest release.")
        return 0
    try:
        data = fetch(f"{base}/{name}")
    except Exception as exc:  # noqa: BLE001
        out(f"clip-harness upgrade: download failed: {exc}")
        return 1
    expected = sums[name]
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        out(f"clip-harness upgrade: checksum mismatch for {name} — "
            f"SHA256SUMS says {expected}, the download hashes to "
            f"{actual}. Aborting: nothing installed.")
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        wheel_path = Path(tmp) / name
        wheel_path.write_bytes(data)
        try:
            (installer or _default_installer)(wheel_path)
        except UpdateError as exc:
            out(f"clip-harness upgrade: {exc}. Nothing was "
                f"installed — the equivalent manual install:")
            out(f"  curl -fLO {base}/{name}")
            out(f"  pipx install --force --python python3.12 "
                f"./{name}")
            return 1
    out(f"clip-harness {current_version} → {latest} — the new "
        f"version takes effect on the next run")
    return 0
