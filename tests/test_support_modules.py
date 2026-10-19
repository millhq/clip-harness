"""Unit tests: goal/task store, capability manifest, settings merge,
receipts, session registry persistence."""

import json
from pathlib import Path

from clip_harness.goals import GoalStore
from clip_harness.manifest import BUILTIN_TOOLS, ManifestStore
from clip_harness.receipts import Ledger
from clip_harness.sessions import RunHandle, SessionRegistry
from clip_harness.settings import load_settings


# -- goals ----------------------------------------------------------------

def test_goal_store_round_trip(tmp_path):
    store = GoalStore(tmp_path, "s1")
    assert store.section().startswith("[goal] (none set)")
    store.set_goal("Ship it")
    store.add_task("one")
    store.add_task("two")
    store.set_task_status("one", "doing")
    section = store.section()
    assert "[goal] Ship it [active]" in section
    assert "[doing] one" in section and "[open] two" in section
    # Persisted: a fresh instance sees the same state.
    again = GoalStore(tmp_path, "s1")
    assert again.goal()["title"] == "Ship it"
    assert len(again.open_tasks()) == 2
    store.set_task_status("one", "done")
    assert "[doing] one" not in store.section()


def test_goal_store_is_per_session(tmp_path):
    GoalStore(tmp_path, "a").set_goal("Goal A")
    assert GoalStore(tmp_path, "b").goal() is None


# -- manifest ---------------------------------------------------------------

def test_manifest_builtins_and_registration(tmp_path):
    store = ManifestStore(tmp_path)
    text, digest = store.current([])
    for tool in BUILTIN_TOOLS:
        assert tool["name"] in text
    assert store.register([{"name": "x.tool", "description": "X"}]) is True
    assert store.register([{"name": "x.tool", "description": "X"}]) is False
    text2, digest2 = store.current([])
    assert "x.tool" in text2 and digest2 != digest


def test_manifest_pending_section_once_per_change(tmp_path):
    store = ManifestStore(tmp_path)
    first = store.pending_section("sess", [])
    assert "— capabilities:" in first
    assert store.pending_section("sess", []) == ""
    store.register([{"name": "new.tool", "description": "N"}])
    refreshed = store.pending_section("sess", [])
    assert "new.tool" in refreshed
    # A different session still gets its first copy.
    assert "— capabilities:" in store.pending_section("other", [])


# -- settings merge -------------------------------------------------------------

def test_settings_merge_user_then_project(tmp_path):
    user = tmp_path / "user.json"
    user.write_text(json.dumps({
        "allowlist": ["git *"], "timeout": 30,
        "hooks": {"PreRun": [{"command": "a.sh"}]},
        "tools": [{"name": "u.tool", "description": "user tool"}],
    }))
    project = tmp_path / "proj" / ".clip-harness"
    project.mkdir(parents=True)
    (project / "settings.json").write_text(json.dumps({
        "allowlist": ["make *"], "timeout": 60,
        "hooks": {"PreRun": [{"command": "b.sh"}]},
    }))
    settings = load_settings(cwd=tmp_path / "proj", user_path=user)
    assert settings.allowlist == ["git *", "make *"]  # concatenated
    assert settings.timeout == 60                     # project wins
    assert [h.command for h in settings.hook_handlers("PreRun")] == \
        ["a.sh", "b.sh"]
    assert settings.tools[0]["name"] == "u.tool"
    assert len(settings.sources) == 2


# -- receipts ---------------------------------------------------------------------

def test_ledger_append_and_read(tmp_path):
    ledger = Ledger(tmp_path / "receipts.jsonl")
    ledger.append("r-1", "PayloadCaptured", {"status": "captured"},
                  binding_id="s1")
    ledger.append("r-1", "PostRun", {"status": "ok"})
    ledger.append("r-2", "PostRun", {"status": "ok"})
    entries = ledger.entries(run_id="r-1")
    assert [e["event"] for e in entries] == ["PayloadCaptured", "PostRun"]
    assert entries[0]["binding_id"] == "s1"
    assert ledger.run_ids() == ["r-1", "r-2"]
    assert ledger.summary() == {"PayloadCaptured": 1, "PostRun": 2}
    # Append-only file with fsync — one JSON object per line.
    lines = (tmp_path / "receipts.jsonl").read_text().splitlines()
    assert len(lines) == 3
    assert all(json.loads(line) for line in lines)


# -- sessions -------------------------------------------------------------------------

def test_registry_binding_persistence_and_cancel_flag(tmp_path):
    registry = SessionRegistry(tmp_path)
    handle = RunHandle(run_id="r-1", command="echo x", cwd="/tmp",
                       binding_id="sess-9")
    registry.register(handle)
    # A fresh registry (another process) sees the persisted binding.
    other = SessionRegistry(tmp_path)
    loaded = other.get("sess-9")
    assert loaded is not None and loaded.run_id == "r-1"
    assert other.request_cancel("sess-9") is True
    assert other.cancel_flag_set("sess-9") is True
    assert other.request_cancel("missing") is False
    assert other.last() is not None


def test_unbound_runs_are_not_addressable(tmp_path):
    registry = SessionRegistry(tmp_path)
    registry.register(RunHandle(run_id="r-2", command="echo y",
                                cwd="/tmp"))
    assert registry.list_bound() == []
    assert registry.last().run_id == "r-2"
