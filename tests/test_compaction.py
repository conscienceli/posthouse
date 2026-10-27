"""Change-history compaction (git gc) keeps disk usage small without losing versions."""

import subprocess

import pytest

from conftest import ADMIN, AGENT
from posthouse.config import Settings
from posthouse.db import DB
from posthouse.storage import Workspaces


@pytest.fixture
def store(tmp_path):
    s = Settings.from_env(data_dir=str(tmp_path / "data"), admin_password="x")
    st = Workspaces(s, DB(s.db_path))
    if not st.git:
        pytest.skip("git not installed")
    return st


def loose_objects(store, ws) -> int:
    out = subprocess.run(["git", "-C", str(store.dir_of(ws)), "count-objects", "-v"],
                         capture_output=True, text=True).stdout
    return int(next(line.split(":")[1] for line in out.splitlines() if line.startswith("count:")))


def busy(store, name="busy", writes=120):
    ws = store.create(name)
    for i in range(writes):
        store.write(ws, f"files/doc{i % 10}.md", f"version {i}\n".encode() * 50, actor="Claude")
    return ws


def test_new_repos_have_no_sample_hooks(store):
    ws = store.create("fresh")
    hooks = store.dir_of(ws) / ".git" / "hooks"
    assert not hooks.exists() or not list(hooks.glob("*.sample"))


def test_compaction_shrinks_history_and_keeps_versions(store):
    ws = busy(store)
    first = store.history(ws, "files/doc3.md", limit=200)[-1]["revision"]
    assert loose_objects(store, ws) >= store.LOOSE_OBJECTS_THRESHOLD
    r = store.compact(ws)
    assert r["compacted"] and r["after_bytes"] < r["before_bytes"] / 3
    assert loose_objects(store, ws) == 0
    assert store.history_bytes(ws) == r["after_bytes"]
    # nothing was lost: the oldest version is still readable and writes keep working
    assert store.read(ws, "files/doc3.md", revision=first)["content"].startswith("version 3\n")
    store.write(ws, "after.md", b"still works", actor="Claude")
    assert store.history(ws, limit=1)[0]["message"] == "Create after.md"


def test_compaction_skips_small_repos_unless_forced(store):
    ws = store.create("small")
    store.write(ws, "a.md", b"x")
    assert store.compact(ws) == {"workspace": "small", "compacted": False, "reason": "not needed",
                                 "before_bytes": store.history_bytes(ws), "after_bytes": store.history_bytes(ws)}
    assert store.compact(ws, force=True)["compacted"]


def test_compact_all(store):
    busy(store, "one")
    store.create("two")
    results = {r["workspace"]: r["compacted"] for r in store.compact_all()}
    assert results == {"one": True, "two": False}


def test_api_reports_size_and_compacts(client, ws):
    for i in range(30):
        client.put("/api/workspaces/demo/raw/notes.md", content=f"v{i}".encode() * 100, headers=AGENT)
    detail = client.get("/api/workspaces/demo", headers=AGENT).json()
    assert detail["history_bytes"] > 0
    assert client.post("/api/workspaces/demo/compact", headers=AGENT).status_code == 403
    r = client.post("/api/workspaces/demo/compact", headers=ADMIN).json()
    assert r["compacted"] and r["after_bytes"] < r["before_bytes"]
    acts = client.get("/api/activity?workspace=demo", headers=ADMIN).json()["activity"]
    assert acts[0]["action"] == "history.compact"


def test_gc_interval_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("POSTHOUSE_GIT_GC_HOURS", "6")
    assert Settings.from_env(data_dir=str(tmp_path), admin_password="x").git_gc_hours == 6


def test_info_reports_compaction_interval(client):
    assert client.get("/api/info").json()["history_compaction_hours"] == 24
