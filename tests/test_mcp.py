import json

from conftest import AGENT, basic, call_tool, mcp

MODERN = "2026-07-28"


def meta(version=MODERN):
    return {"io.modelcontextprotocol/protocolVersion": version,
            "io.modelcontextprotocol/clientInfo": {"name": "pytest-client", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {}}


def test_unauthenticated_gets_oauth_challenge(client):
    r = mcp(client, "tools/list")
    assert r.status_code == 401
    assert 'resource_metadata="https://posthouse.test/.well-known/oauth-protected-resource/mcp"' in r.headers["www-authenticate"]
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401 and 'error="invalid_token"' in r.headers["www-authenticate"]


def test_legacy_handshake(client):
    r = mcp(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "x", "version": "1"}}, headers=AGENT)
    res = r.json()["result"]
    assert res["protocolVersion"] == "2025-06-18" and "tools" in res["capabilities"] and res["instructions"]
    assert "mcp-session-id" not in r.headers
    r = mcp(client, "initialize", {"protocolVersion": "1999-01-01"}, headers=AGENT)
    assert r.json()["result"]["protocolVersion"] == "2025-11-25"
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=AGENT)
    assert r.status_code == 202
    assert mcp(client, "ping", headers=AGENT).json()["result"]["resultType"] == "complete"


def test_modern_discover_and_headers(client):
    h = {**AGENT, "MCP-Protocol-Version": MODERN, "Mcp-Method": "server/discover"}
    r = mcp(client, "server/discover", {"_meta": meta()}, headers=h)
    res = r.json()["result"]
    assert MODERN in res["supportedVersions"] and res["_meta"]["io.modelcontextprotocol/serverInfo"]["name"]
    r = mcp(client, "tools/list", {"_meta": meta()}, headers={**AGENT, "Mcp-Method": "tools/call"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32020
    r = mcp(client, "tools/list", {"_meta": meta("2099-01-01")}, headers=AGENT)
    assert r.status_code == 400 and r.json()["error"]["code"] == -32022
    assert MODERN in r.json()["error"]["data"]["supported"]
    r = mcp(client, "nope/method", {"_meta": meta()}, headers=AGENT)
    assert r.status_code == 404 and r.json()["error"]["code"] == -32601
    r = mcp(client, "tools/list", {"_meta": meta()}, headers=AGENT)
    res = r.json()["result"]
    assert res["cacheScope"] == "private" and res["ttlMs"] > 0
    # base64-encoded Mcp-Name is decoded before comparison
    import base64
    name_hdr = "=?base64?" + base64.b64encode(b"list_workspaces").decode() + "?="
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                  "params": {"name": "list_workspaces", "arguments": {}, "_meta": meta()}},
                    headers={**AGENT, "MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/call", "Mcp-Name": name_hdr})
    assert r.status_code == 200 and not r.json()["result"]["isError"]


def test_tools_by_role(client, ws):
    names = [t["name"] for t in mcp(client, "tools/list", headers=AGENT).json()["result"]["tools"]]
    assert "create_workspace" in names and "start_session" in names
    W = basic("demo", "ws-pw")
    tools = mcp(client, "tools/list", headers=W).json()["result"]["tools"]
    assert "create_workspace" not in [t["name"] for t in tools]
    start = next(t for t in tools if t["name"] == "start_session")
    assert "workspace" not in start["inputSchema"]["required"]
    read = next(t for t in tools if t["name"] == "read_file")
    assert read["annotations"]["readOnlyHint"] is True


def test_tool_flow(client, ws):
    H = {**AGENT, "X-Posthouse-Agent": "Claude"}
    text, err = call_tool(client, H, "start_session", {"workspace": "demo"})
    assert not err and text.startswith("# Workspace: demo") and "STATUS.md" in text
    text, err = call_tool(client, H, "write_file", {"workspace": "demo", "path": "files/plan.md", "content": "v1"})
    version = json.loads(text)["version"]
    text, err = call_tool(client, H, "read_file", {"workspace": "demo", "path": "files/plan.md"})
    assert f"version: {version}" in text and text.endswith("v1")
    text, err = call_tool(client, H, "write_file", {"workspace": "demo", "path": "files/plan.md", "content": "v2",
                                                    "expected_version": "0000000000000000"})
    assert err and "changed by someone else" in text
    call_tool(client, H, "edit_file", {"workspace": "demo", "path": "files/plan.md", "old_text": "v1", "new_text": "v1.1"})
    call_tool(client, H, "append_file", {"workspace": "demo", "path": "files/plan.md", "content": "more"})
    call_tool(client, H, "move_file", {"workspace": "demo", "source": "files/plan.md", "destination": "plan.md"})
    text, _ = call_tool(client, H, "list_files", {"workspace": "demo"})
    assert "plan.md" in text
    text, _ = call_tool(client, H, "search", {"query": "more"})
    results = json.loads(text)["results"]
    assert results[0]["id"] == "demo/plan.md" and results[0]["url"].startswith("https://posthouse.test/#/w/demo")
    text, _ = call_tool(client, H, "fetch", {"id": "demo/plan.md"})
    assert json.loads(text)["text"] == "v1.1\nmore"
    text, err = call_tool(client, H, "log_progress", {"workspace": "demo", "summary": "Planned", "next_steps": "Do it",
                                                      "status": "# Status\nplanned"})
    assert not err
    text, _ = call_tool(client, H, "history", {"workspace": "demo", "limit": 3})
    assert "Claude" in text and "Progress" in text
    text, err = call_tool(client, H, "delete_file", {"workspace": "demo", "path": "plan.md"})
    assert not err
    text, err = call_tool(client, H, "read_file", {"workspace": "missing", "path": "x"})
    assert err and "not found" in text
    text, err = call_tool(client, H, "create_workspace", {"name": "新项目", "description": "via MCP"})
    assert not err and "新项目" in text


def test_workspace_scoped_credential(client, ws):
    client.post("/api/workspaces", json={"name": "secret"}, headers=AGENT)
    W = basic("demo", "ws-pw")
    text, err = call_tool(client, W, "write_file", {"path": "x.md", "content": "no workspace arg needed"})
    assert not err
    text, err = call_tool(client, W, "read_file", {"workspace": "secret", "path": "AGENTS.md"})
    assert err
    text, _ = call_tool(client, W, "list_workspaces")
    assert [w["name"] for w in json.loads(text)["workspaces"]] == ["demo"]
    text, _ = call_tool(client, W, "search", {"query": "workspace"})
    assert all(r["workspace"] == "demo" for r in json.loads(text)["results"])


def test_unknown_tool_and_bad_json(client):
    r = mcp(client, "tools/call", {"name": "nope", "arguments": {}}, headers=AGENT)
    assert r.json()["error"]["code"] == -32602
    r = client.post("/mcp", content=b"{not json", headers={**AGENT, "Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32700


def test_batch_and_get(client):
    r = client.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                  {"jsonrpc": "2.0", "method": "notifications/initialized"},
                                  {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}], headers=AGENT)
    assert [m["id"] for m in r.json()] == [1, 2]
    assert client.get("/mcp", headers=AGENT).status_code == 405


def test_url_token(client, ws):
    t = client.post("/api/tokens", json={"name": "GPT", "role": "workspace", "workspace": "demo"},
                    headers=basic("admin", "admin-pw-123")).json()
    text, err = call_tool(client, {}, "write_file", {"path": "t.md", "content": "via url"}, path=f"/mcp/t/{t['token']}")
    assert not err
    assert mcp(client, "tools/list", path="/mcp/t/ph_invalid").status_code == 401
    hist = client.get("/api/workspaces/demo/history", headers=AGENT).json()["commits"]
    assert hist[0]["author"] == "GPT"


def test_modern_client_info_names_author(client, ws):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "write_file", "_meta": meta(),
                                             "arguments": {"workspace": "demo", "path": "m.md", "content": "x"}}},
                    headers=AGENT)
    assert not r.json()["result"]["isError"]
    assert client.get("/api/workspaces/demo/history", headers=AGENT).json()["commits"][0]["author"] == "pytest-client"
