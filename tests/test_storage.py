import pytest

from caw.config import Settings
from caw.db import DB
from caw.storage import Conflict, Forbidden, NotFound, StorageError, Workspaces, validate_name


@pytest.fixture
def store(tmp_path):
    s = Settings.from_env(data_dir=str(tmp_path / "data"), admin_password="x")
    return Workspaces(s, DB(s.db_path))


@pytest.mark.parametrize("name", ["a", "paper-2026", "论文项目", "a.b_c-d", "x" * 64])
def test_valid_names(name):
    assert validate_name(name) == name


@pytest.mark.parametrize("name", ["", "_a", "a_", "-a", "a/b", "../x", "x" * 65, "admin", "Agent", "a b", "a..b"])
def test_invalid_names(name):
    with pytest.raises(StorageError):
        validate_name(name)


def test_create_has_template_and_history(store):
    ws = store.create("proj", "desc", actor="tester")
    files = {e["path"] for e in store.tree(ws, "", recursive=True)["entries"]}
    assert {"AGENTS.md", "STATUS.md", "DECISIONS.md", "journal", "memory", "files"} <= files
    assert "proj" in store.read(ws, "AGENTS.md")["content"]
    if store.git:
        assert store.history(ws)[0]["author"] == "tester"


def test_duplicate_create(store):
    store.create("proj")
    with pytest.raises(Conflict):
        store.create("proj")


@pytest.mark.parametrize("bad", ["../x", "a/../../b", ".git/config", "files/.git/x", "x\x00y"])
def test_path_safety(store, bad):
    ws = store.create("proj")
    with pytest.raises((StorageError, Forbidden)):
        store.write(ws, bad, b"x")


def test_leading_slash_is_relative(store):
    ws = store.create("proj")
    store.write(ws, "/notes/a.md", b"hi")
    assert store.read(ws, "notes/a.md")["content"] == "hi"


def test_write_conflict_and_new(store):
    ws = store.create("proj")
    v1 = store.write(ws, "a.md", b"one")["version"]
    v2 = store.write(ws, "a.md", b"two", expected_version=v1)["version"]
    with pytest.raises(Conflict) as e:
        store.write(ws, "a.md", b"three", expected_version=v1)
    assert e.value.data["current_version"] == v2
    with pytest.raises(Conflict):
        store.write(ws, "a.md", b"x", expected_version="new")
    store.write(ws, "b.md", b"x", expected_version="new")


def test_edit_and_append(store):
    ws = store.create("proj")
    store.write(ws, "a.md", "alpha beta alpha\n".encode())
    with pytest.raises(StorageError):
        store.edit(ws, "a.md", "alpha", "gamma")  # ambiguous
    with pytest.raises(StorageError):
        store.edit(ws, "a.md", "missing", "x")
    store.edit(ws, "a.md", "beta", "β")
    store.edit(ws, "a.md", "alpha", "A", replace_all=True)
    store.append(ws, "a.md", "tail")
    assert store.read(ws, "a.md")["content"] == "A β A\ntail"


def test_read_ranges_and_binary(store):
    ws = store.create("proj")
    store.write(ws, "lines.txt", b"1\n2\n3\n4\n5\n")
    r = store.read(ws, "lines.txt", offset=2, limit=2)
    assert r["content"] == "2\n3\n" and r["total_lines"] == 5
    store.write(ws, "img.bin", bytes(range(256)))
    assert store.read(ws, "img.bin")["encoding"] == "base64"


def test_size_limit(tmp_path):
    s = Settings.from_env(data_dir=str(tmp_path / "d"), admin_password="x")
    s.max_file_bytes = 10
    store = Workspaces(s, DB(s.db_path))
    ws = store.create("p", template=False)
    with pytest.raises(StorageError) as e:
        store.write(ws, "big", b"x" * 11)
    assert e.value.status == 413


def test_move_delete_and_history_restore(store):
    ws = store.create("proj")
    store.write(ws, "d/a.md", b"v1", actor="A")
    store.write(ws, "d/a.md", b"v2", actor="B")
    store.move(ws, "d", "e")
    with pytest.raises(NotFound):
        store.read(ws, "d/a.md")
    assert store.read(ws, "e/a.md")["content"] == "v2"
    with pytest.raises(StorageError):
        store.delete_path(ws, "e")  # non-empty without recursive
    store.delete_path(ws, "e", recursive=True)
    if store.git:
        commits = store.history(ws, "d/a.md")  # newest first: move, B's write, A's write
        assert [c["author"] for c in commits[1:3]] == ["B", "A"]
        old = store.read(ws, "d/a.md", revision=commits[2]["revision"])
        assert old["content"] == "v1"
        with pytest.raises(StorageError):
            store.read(ws, "d/a.md", revision="HEAD; rm -rf /")


def test_rename_trash_restore_purge(store):
    ws = store.create("proj", "desc", password="pw")
    store.update("proj", new_name="proj2", description="new")
    with pytest.raises(NotFound):
        store.get("proj")
    trash_id = store.delete("proj2")["trash_id"]
    assert store.trash_list()[0]["name"] == "proj2"
    restored = store.restore(trash_id, "proj3")
    assert restored["description"] == "new" and restored["password_hash"]
    trash_id = store.delete("proj3")["trash_id"]
    store.purge(trash_id)
    assert store.trash_list() == []
    assert ws["id"]


def test_search_ranks_phrase_first(store):
    ws = store.create("proj", template=False)
    store.write(ws, "a.md", b"apple\nbanana\napple pie\n")
    store.write(ws, "b.md", b"apple and some pie elsewhere")
    store.write(ws, "c.md", b"nothing here")
    res = store.search("apple pie", [ws])
    assert [r["path"] for r in res][:2] == ["a.md", "b.md"]
    assert res[0]["matches"][0]["text"] == "apple pie"
    assert all(r["path"] != "c.md" for r in res)


def test_session_bundle_and_progress(store):
    ws = store.create("proj", "my project")
    store.log_progress(ws, "Claude", "Did the outline", "Write intro", status="# Status\nOutline done")
    b = store.session_bundle(ws)
    assert b["status_md"].startswith("# Status\nOutline done")
    assert "Did the outline" in b["recent_journal"][0]["content"]
    assert "AGENTS.md" in b["files"]


def test_sync_imports_existing_directories(tmp_path):
    s = Settings.from_env(data_dir=str(tmp_path / "d"), admin_password="x")
    (s.workspaces_dir / "manual").mkdir(parents=True)
    (s.workspaces_dir / "manual" / "notes.md").write_text("hello")
    store = Workspaces(s, DB(s.db_path))
    ws = store.get("manual")
    assert store.read(ws, "notes.md")["content"] == "hello"
