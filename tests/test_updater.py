"""The update pattern: version compare, feed parsing, the daily
throttle, and the fail-closed verified upgrade. No network anywhere
— fetchers and installers are injected seams, and the one real wheel
is a tiny zip built in-test."""

from __future__ import annotations

import hashlib
import io
import json
import time
import zipfile
from pathlib import Path

from clip_harness import __version__, updater

NOW = 1_760_000_000.0
DAY = 24 * 3600


# ----------------------------------------------------------------------
# Version compare
# ----------------------------------------------------------------------

def test_parse_version_shapes():
    assert updater.parse_version("0.2.1") == (0, 2, 1)
    assert updater.parse_version("v0.2.1") == (0, 2, 1)
    assert updater.parse_version("0.2") == (0, 2, 0)
    assert updater.parse_version("1") == (1, 0, 0)


def test_is_newer():
    assert updater.is_newer("0.2.1", "0.2.0")
    assert updater.is_newer("0.3.0", "0.2.9")
    assert updater.is_newer("v1.0.0", "0.9.9")
    assert not updater.is_newer("0.2.0", "0.2.0")
    assert not updater.is_newer("0.2", "0.2.0")  # equal, not newer
    assert not updater.is_newer("0.1.9", "0.2.0")


# ----------------------------------------------------------------------
# Feed parsing
# ----------------------------------------------------------------------

SUMS = (b"aaaa  clip_harness-9.9.9-py3-none-any.whl\n"
        b"bbbb  clip_harness-9.9.9.tar.gz\n")


def test_parse_checksums_finds_wheel_and_sdist():
    sums = updater.parse_checksums(SUMS)
    assert sums["clip_harness-9.9.9-py3-none-any.whl"] == "aaaa"
    assert sums["clip_harness-9.9.9.tar.gz"] == "bbbb"


def test_wheel_version_ignores_sdist():
    assert updater.wheel_version(
        "clip_harness-9.9.9-py3-none-any.whl") == "9.9.9"
    assert updater.wheel_version("clip_harness-9.9.9.tar.gz") is None
    assert updater.wheel_version("SHA256SUMS") is None


def test_notice_line_shape():
    assert updater.notice_line("0.3.0", "0.2.1") == (
        "[update] clip-harness 0.3.0 is available (you run 0.2.1) — "
        "upgrade with: clip-harness upgrade")


# ----------------------------------------------------------------------
# The throttled check
# ----------------------------------------------------------------------

class CountingFetcher:
    def __init__(self, data: bytes = SUMS, fail: bool = False):
        self.data = data
        self.fail = fail
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if self.fail:
            raise OSError("offline")
        return self.data


def _seed_cache(state: Path, latest, checked_at: float):
    state.mkdir(parents=True, exist_ok=True)
    (state / "update-check.json").write_text(
        json.dumps({"checked_at": checked_at, "latest": latest}),
        encoding="utf-8")


def test_fresh_cache_skips_fetch(tmp_path):
    fetcher = CountingFetcher()
    _seed_cache(tmp_path, "9.9.9", NOW - 60)
    assert updater.check_for_update(tmp_path, "0.2.0", now=NOW,
                                    fetcher=fetcher) == "9.9.9"
    assert fetcher.calls == []


def test_fresh_cache_not_newer_returns_none(tmp_path):
    fetcher = CountingFetcher()
    _seed_cache(tmp_path, "0.2.0", NOW - 60)
    assert updater.check_for_update(tmp_path, "0.2.0", now=NOW,
                                    fetcher=fetcher) is None
    assert fetcher.calls == []


def test_stale_cache_fetches_and_recaches(tmp_path):
    fetcher = CountingFetcher()
    _seed_cache(tmp_path, None, NOW - DAY - 1)
    assert updater.check_for_update(tmp_path, "0.2.0", now=NOW,
                                    fetcher=fetcher) == "9.9.9"
    assert len(fetcher.calls) == 1
    cache = json.loads((tmp_path / "update-check.json")
                       .read_text(encoding="utf-8"))
    assert cache == {"checked_at": NOW, "latest": "9.9.9"}


def test_fetch_failure_with_stale_cache_serves_cached_verdict(tmp_path):
    fetcher = CountingFetcher(fail=True)
    _seed_cache(tmp_path, "9.9.9", NOW - DAY - 1)
    assert updater.check_for_update(tmp_path, "0.2.0", now=NOW,
                                    fetcher=fetcher) == "9.9.9"


def test_fetch_failure_without_cache_returns_none(tmp_path):
    fetcher = CountingFetcher(fail=True)
    assert updater.check_for_update(tmp_path, "0.2.0", now=NOW,
                                    fetcher=fetcher) is None


def test_no_cache_successful_fetch(tmp_path):
    fetcher = CountingFetcher()
    assert updater.check_for_update(tmp_path, "9.9.8", now=NOW,
                                    fetcher=fetcher) == "9.9.9"


def test_force_bypasses_throttle(tmp_path):
    fetcher = CountingFetcher()
    _seed_cache(tmp_path, None, NOW - 60)
    assert updater.check_for_update(tmp_path, "0.2.0", now=NOW,
                                    fetcher=fetcher,
                                    force=True) == "9.9.9"
    assert len(fetcher.calls) == 1


def test_real_clock_default_now(tmp_path):
    # No `now` passed: a just-seeded cache counts as fresh.
    _seed_cache(tmp_path, "9.9.9", time.time())
    assert updater.check_for_update(
        tmp_path, "0.2.0", fetcher=CountingFetcher()) == "9.9.9"


# ----------------------------------------------------------------------
# Upgrade
# ----------------------------------------------------------------------

def _tiny_wheel(version: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("clip_harness/__init__.py",
                    f'__version__ = "{version}"\n')
        zf.writestr(f"clip_harness-{version}.dist-info/METADATA",
                    f"Metadata-Version: 2.1\nName: clip-harness\n"
                    f"Version: {version}\n")
        zf.writestr(f"clip_harness-{version}.dist-info/WHEEL",
                    "Wheel-Version: 1.0\n")
    return buf.getvalue()


class Feed:
    """A fetcher backed by {filename: bytes}."""

    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        name = url.rsplit("/", 1)[-1]
        if name not in self.files:
            raise OSError(f"no such asset {name}")
        return self.files[name]


def _feed_for(version: str, wheel: bytes | None = None,
              digest: str | None = None) -> Feed:
    wheel = _tiny_wheel(version) if wheel is None else wheel
    name = f"clip_harness-{version}-py3-none-any.whl"
    digest = hashlib.sha256(wheel).hexdigest() if digest is None \
        else digest
    sums = (f"{digest}  {name}\n"
            f"{'0' * 64}  clip_harness-{version}.tar.gz\n") \
        .encode("utf-8")
    return Feed({"SHA256SUMS": sums, name: wheel})


class RecordingInstaller:
    def __init__(self):
        self.installed: list[tuple[str, str]] = []

    def __call__(self, wheel_path: Path) -> None:
        data = Path(wheel_path).read_bytes()
        self.installed.append(
            (Path(wheel_path).name, hashlib.sha256(data).hexdigest()))


def test_upgrade_happy_path_installs_the_verified_wheel():
    wheel = _tiny_wheel("99.0.0")
    feed = _feed_for("99.0.0", wheel=wheel)
    installer = RecordingInstaller()
    messages: list[str] = []
    rc = updater.upgrade(__version__, fetcher=feed,
                         installer=installer, out=messages.append)
    assert rc == 0
    expected = hashlib.sha256(wheel).hexdigest()
    assert installer.installed == [
        ("clip_harness-99.0.0-py3-none-any.whl", expected)]
    assert any(f"{__version__} → 99.0.0" in m for m in messages)


def test_upgrade_checksum_mismatch_installs_nothing():
    feed = _feed_for("99.0.0", digest="f" * 64)
    installer = RecordingInstaller()
    messages: list[str] = []
    rc = updater.upgrade(__version__, fetcher=feed,
                         installer=installer, out=messages.append)
    assert rc == 1
    assert installer.installed == []
    assert any("checksum mismatch" in m for m in messages)


def test_upgrade_already_latest_installs_nothing():
    feed = _feed_for(__version__)
    installer = RecordingInstaller()
    messages: list[str] = []
    rc = updater.upgrade(__version__, fetcher=feed,
                         installer=installer, out=messages.append)
    assert rc == 0
    assert installer.installed == []
    assert messages == [
        f"clip-harness {__version__} is already the latest release."]


def test_upgrade_feed_without_wheel_line_aborts():
    feed = Feed({"SHA256SUMS":
                 b"abcd  clip_harness-99.0.0.tar.gz\n"})
    installer = RecordingInstaller()
    rc = updater.upgrade(__version__, fetcher=feed,
                         installer=installer, out=lambda _m: None)
    assert rc == 1
    assert installer.installed == []


def test_upgrade_unreachable_feed_aborts():
    def boom(url: str) -> bytes:
        raise OSError("offline")

    rc = updater.upgrade(__version__, fetcher=boom,
                         installer=RecordingInstaller(),
                         out=lambda _m: None)
    assert rc == 1


# ----------------------------------------------------------------------
# Packaging: both entry points, same main
# ----------------------------------------------------------------------

def test_pyproject_registers_both_scripts():
    import tomllib
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as fh:
        project = tomllib.load(fh)["project"]
    scripts = project["scripts"]
    assert scripts["clip-harness"] == "clip_harness.cli:main"
    assert scripts["clip"] == "clip_harness.cli:main"
