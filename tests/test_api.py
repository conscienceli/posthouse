import pytest

from conftest import ADMIN, ADMIN_PW, AGENT, basic


def test_info_and_me(client):
    info = client.get("/api/info").json()
    assert info["mcp_url"] == "https://posthouse.test/mcp" and info["agent_password_enabled"]
    assert client.get("/api/me").json() == {"authenticated": False}
    me = client.get("/api/me", headers=AGENT).json()
    assert me["role"] == "agent" and me["authenticated"]


def test_requires_auth(client):
    assert client.get("/api/workspaces").status_code == 401
    assert client.get("/api/workspaces", headers=basic("agent", "nope")).status_code == 401


def test_levels(client, ws):
    W = basic("demo", "ws-pw")
    client.post("/api/workspaces", json={"name": "other"}, headers=AGENT)
    assert [w["name"] for w in client.get("/api/workspaces", headers=W).json()["workspaces"]] == ["demo"]
    assert client.post("/api/workspaces", json={"name": "x"}, headers=W).status_code == 403
    assert client.get("/api/workspaces/other/tree", headers=W).status_code == 404  # hidden, not forbidden
    assert client.delete("/api/workspaces/demo", headers=AGENT).status_code == 403
    assert client.patch("/api/workspaces/demo", json={"password": "x"}, headers=AGENT).status_code == 403
    assert client.patch("/api/workspaces/demo", json={"description": "d2"}, headers=AGENT).status_code == 200
    assert client.patch("/api/workspaces/demo", json={"description": "d3"}, headers=W).status_code == 403
    assert client.get("/api/tokens", headers=AGENT).status_code == 403
    # the agent password also works with a workspace name as the user
    assert client.get("/api/workspaces/other", headers=basic("demo", "agent-pw-456")).status_code == 200


def test_file_endpoints(client, ws):
    H = {**basic("demo", "ws-pw"), "X-Posthouse-Agent": "Tester"}
    r = client.put("/api/workspaces/demo/file", json={"path": "files/a.md", "content": "hello"}, headers=H)
    v = r.json()["version"]
    assert client.get("/api/workspaces/demo/file", params={"path": "files/a.md"}, headers=H).json()["content"] == "hello"
    r = client.put("/api/workspaces/demo/file", json={"path": "files/a.md", "content": "x", "expected_version": "0" * 16},
                   headers=H)
    assert r.status_code == 409 and r.json()["current_version"] == v
    assert client.post("/api/workspaces/demo/edit", json={"path": "files/a.md", "old_text": "hello",
                                                         "new_text": "hi"}, headers=H).status_code == 200
    client.post("/api/workspaces/demo/append", json={"path": "files/a.md", "content": "more"}, headers=H)
    client.post("/api/workspaces/demo/mkdir", json={"path": "dir"}, headers=H)
    client.post("/api/workspaces/demo/move", json={"source": "files/a.md", "destination": "dir/a.md"}, headers=H)
    raw = client.get("/api/workspaces/demo/raw/dir/a.md", headers=H)
    assert raw.text == "hi\nmore" and raw.headers["x-posthouse-version"]
    assert client.delete("/api/workspaces/demo/file", params={"path": "dir", "recursive": "true"},
                         headers=H).status_code == 200
    hist = client.get("/api/workspaces/demo/history", headers=H).json()["commits"]
    assert hist[0]["author"] == "Tester"


def test_raw_put_if_match_and_append(client, ws):
    r = client.put("/api/workspaces/demo/raw/notes.txt", content=b"one", headers=AGENT)
    v = r.json()["version"]
    assert client.put("/api/workspaces/demo/raw/notes.txt", content=b"two", headers={**AGENT, "If-Match": '"bad"'}
                      ).status_code == 409
    assert client.put("/api/workspaces/demo/raw/notes.txt", content=b"two", headers={**AGENT, "If-Match": v}
                      ).status_code == 200
    assert client.put("/api/workspaces/demo/raw/notes.txt", content=b"x", headers={**AGENT, "If-None-Match": "*"}
                      ).status_code == 409
    client.put("/api/workspaces/demo/raw/notes.txt?append=true", content="三".encode(), headers=AGENT)
    assert client.get("/api/workspaces/demo/raw/notes.txt", headers=AGENT).text == "two\n三"


def test_html_is_not_served_as_html(client, ws):
    client.put("/api/workspaces/demo/raw/x.html", content=b"<script>alert(1)</script>", headers=AGENT)
    r = client.get("/api/workspaces/demo/raw/x.html", headers=AGENT)
    assert r.headers["content-type"].startswith("text/plain")


def test_upload_archive_session_progress_search(client, ws):
    files = [("files", ("a.txt", b"alpha", "text/plain")), ("files", ("b.bin", bytes(range(10)), "application/x"))]
    r = client.post("/api/workspaces/demo/upload", data={"dir": "up"}, files=files, headers=AGENT)
    assert [u["path"] for u in r.json()["uploaded"]] == ["up/a.txt", "up/b.bin"]
    z = client.get("/api/workspaces/demo/archive", headers=AGENT)
    assert z.headers["content-type"] == "application/zip" and len(z.content) > 100
    client.post("/api/workspaces/demo/progress", json={"summary": "Did things", "status": "# S\nnew"}, headers=AGENT)
    s = client.get("/api/workspaces/demo/session", headers=AGENT).json()
    assert s["status_md"].startswith("# S")
    md = client.get("/api/workspaces/demo/session?format=md", headers=AGENT).text
    assert md.startswith("# Workspace: demo")
    res = client.get("/api/search", params={"q": "alpha"}, headers=AGENT).json()["results"]
    assert res[0]["id"] == "demo/up/a.txt"


def test_admin_session_csrf_and_tokens(client, ws):
    assert client.post("/api/login", json={"password": "bad"}).status_code == 401
    assert client.post("/api/login", json={"password": ADMIN_PW}).status_code == 200
    assert client.get("/api/me").json()["role"] == "admin"
    assert client.post("/api/workspaces", json={"name": "y"}).status_code == 403  # missing CSRF header
    csrf = {"X-Posthouse-CSRF": "1"}
    t = client.post("/api/tokens", json={"name": "ChatGPT", "role": "workspace", "workspace": "demo"},
                    headers=csrf).json()
    assert t["token"].startswith("ph_") and t["mcp_url_with_token"].endswith(t["token"])
    tok = {"Authorization": "Bearer " + t["token"]}
    client.cookies.clear()
    assert [w["name"] for w in client.get("/api/workspaces", headers=tok).json()["workspaces"]] == ["demo"]
    client.put("/api/workspaces/demo/raw/t.md", content=b"x", headers=tok)
    assert client.get("/api/workspaces/demo/history", headers=tok).json()["commits"][0]["author"] == "ChatGPT"
    tokens = client.get("/api/tokens", headers=ADMIN).json()["tokens"]
    assert tokens[0]["last_used_at"]
    assert client.delete(f"/api/tokens/{tokens[0]['id']}", headers=ADMIN).status_code == 200
    assert client.get("/api/workspaces", headers=tok).status_code == 401


def test_trash_and_activity(client, ws):
    assert client.delete("/api/workspaces/demo", headers=ADMIN).status_code == 200
    trash = client.get("/api/trash", headers=ADMIN).json()["trash"]
    r = client.post(f"/api/trash/{trash[0]['trash_id']}/restore", json={"name": "demo2"}, headers=ADMIN)
    assert r.json()["name"] == "demo2" and r.json()["has_password"]
    acts = [a["action"] for a in client.get("/api/activity", headers=ADMIN).json()["activity"]]
    assert "workspace.delete" in acts and "workspace.restore" in acts
    W = basic("demo2", "ws-pw")
    assert all(a["workspace"] == "demo2" for a in client.get("/api/activity", headers=W).json()["activity"])


def test_rate_limit(client):
    for _ in range(10):
        client.get("/api/workspaces", headers=basic("agent", "wrong"))
    assert client.get("/api/workspaces", headers=AGENT).status_code == 429


def test_ui_served_with_csp(client):
    r = client.get("/")
    assert r.status_code == 200 and "app.js" in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert client.get("/static/i18n/languages.json").status_code == 200


@pytest.mark.parametrize("sent, expected", [
    ("UTF-8''%E6%9B%B4%E6%96%B0%E8%AE%A1%E5%88%92", "更新计划"),        # RFC 8187 (posthouse.py)
    ("utf-8''50%25%20done", "50% done"),
    ("更新计划".encode("utf-8"), "更新计划"),                              # raw UTF-8 bytes (curl)
    ("50% done", "50% done"),                                              # plain ASCII stays as is
    ("café".encode("latin-1"), "café"),                                    # real Latin-1 stays as is
])
def test_text_headers_accept_non_ascii(client, ws, sent, expected):
    r = client.put("/api/workspaces/demo/raw/h.md", content=expected.encode() + b"!",
                   headers={**AGENT, "X-Posthouse-Message": sent, "X-Posthouse-Agent": sent})
    assert r.status_code in (200, 201), r.text
    commit = client.get("/api/workspaces/demo/history?limit=1", headers=AGENT).json()["commits"][0]
    assert commit["message"] == expected
    assert commit["author"] == expected
