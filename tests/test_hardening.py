"""Regression tests for issues found in the security review."""

import logging
import re

from caw import oauth
from caw.__main__ import RedactTokens
from conftest import ADMIN, AGENT, AGENT_PW, basic, mcp, pkce_pair


def test_active_content_is_never_served_inline(client, ws):
    for name, body in [("x.xhtml", b'<html xmlns="http://www.w3.org/1999/xhtml"><script>alert(1)</script></html>'),
                       ("x.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"),
                       ("x.html", b"<script>alert(1)</script>"), ("x.xml", b"<a/>"), ("x.pdf", b"%PDF-1.4\x00\x01")]:
        client.put(f"/api/workspaces/demo/raw/{name}", content=body, headers=AGENT)
        r = client.get(f"/api/workspaces/demo/raw/{name}", headers=AGENT)
        ctype = r.headers["content-type"]
        assert ctype.startswith("text/plain") or ctype == "application/octet-stream", (name, ctype)
        assert "sandbox" in r.headers["content-security-policy"]
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
    client.put("/api/workspaces/demo/raw/i.png", content=png, headers=AGENT)
    r = client.get("/api/workspaces/demo/raw/i.png", headers=AGENT)
    assert r.headers["content-type"] == "image/png" and r.headers["content-disposition"].startswith("inline")
    r = client.get("/api/workspaces/demo/raw/x.pdf", headers=AGENT)
    assert r.headers["content-disposition"].startswith("attachment")


def test_success_does_not_reset_failures(client, ws):
    W = basic("demo", "ws-pw")
    for i in range(12):
        client.get("/api/workspaces", headers=basic("admin", f"guess{i}"))
        client.get("/api/workspaces", headers=W)
    assert client.get("/api/workspaces", headers=basic("admin", "guess-final")).status_code == 429


def test_url_tokens_are_redacted_from_access_log():
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "POST", "/mcp/t/caw_SECRET123", "1.1", 200), None)
    RedactTokens().filter(rec)
    assert "caw_SECRET123" not in rec.getMessage() and "/mcp/t/***" in rec.getMessage()


def test_stale_workspace_record_cannot_resurrect_folder(app, ws):
    store = app.state.ctx.store
    stale = store.get("demo")
    store.delete("demo")
    import pytest
    from caw.storage import NotFound
    with pytest.raises(NotFound):
        store.write(stale, "x.md", b"late write")
    assert not (store.root / "demo").exists()


def test_malformed_inputs_are_client_errors(client, ws):
    r = mcp(client, "tools/call", {"name": {"x": 1}, "arguments": {}}, headers=AGENT)
    assert r.status_code == 200 and r.json()["error"]["code"] == -32602
    r = client.put("/api/workspaces/demo/file", json={"path": "AGENTS.md/x", "content": "x"}, headers=AGENT)
    assert r.status_code == 400
    r = client.put("/api/workspaces/demo/file", json={"path": "a" * 300 + ".md", "content": "x"}, headers=AGENT)
    assert r.status_code == 400
    client.post("/api/workspaces/demo/mkdir", json={"path": "d"}, headers=AGENT)
    r = client.post("/api/workspaces/demo/move", json={"source": "d", "destination": "AGENTS.md", "overwrite": True},
                    headers=AGENT)
    assert r.status_code == 409
    r = client.put("/api/workspaces/demo/file", json={"path": "n.md", "content": "x", "message": "bad\x00msg"},
                   headers=AGENT)
    assert r.status_code == 200
    r = client.post("/api/workspaces/demo/progress", json={"summary": "s", "agent": "evil\x00name"}, headers=AGENT)
    assert r.status_code == 200
    assert client.get("/api/workspaces/demo/history", headers=AGENT).json()["commits"][0]["message"].startswith("Progress")


def test_oauth_malformed_token_requests(client):
    reg = client.post("/oauth/register", json={"redirect_uris": ["https://a.example/cb"],
                                               "token_endpoint_auth_method": "none"}).json()
    r = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": "x", "client_id": reg["client_id"],
                                          "code_verifier": "é" * 50})
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    r = client.post("/oauth/token", files={"client_id": ("f.txt", b"x")}, data={"grant_type": "refresh_token"})
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"


def test_login_page_placeholder_injection(client):
    reg = client.post("/oauth/register", json={"client_name": "X", "redirect_uris": ["https://a.example/cb"],
                                               "token_endpoint_auth_method": "none"}).json()
    _, ch = pkce_pair()
    page = client.get("/oauth/authorize", params={"response_type": "code", "client_id": reg["client_id"],
                                                  "redirect_uri": "https://a.example/cb", "code_challenge": ch,
                                                  "code_challenge_method": "S256"})
    rid = re.search(r'name="request_id" value="([^"]+)"', page.text).group(1)
    r = client.post("/oauth/authorize", data={"request_id": rid, "password": "wrong", "workspace": "{l_workspace}",
                                              "action": "approve"})
    assert 'value="{l_workspace}"' in r.text


def test_cimd_rejects_non_global_addresses():
    assert oauth._global_ips("100.64.0.1", 443) == []      # carrier-grade NAT (e.g. Tailscale)
    assert oauth._global_ips("10.0.0.1", 443) == []
    assert oauth._global_ips("169.254.169.254", 443) == []  # cloud metadata
    assert oauth._global_ips("8.8.8.8", 443) == ["8.8.8.8"]


def test_workspace_password_cache_still_checks_password(client, ws):
    W = basic("demo", "ws-pw")
    assert client.get("/api/workspaces", headers=W).status_code == 200
    assert client.get("/api/workspaces", headers=W).status_code == 200  # cached path
    assert client.get("/api/workspaces", headers=basic("demo", "ws-pw-wrong")).status_code == 401
    client.patch("/api/workspaces/demo", json={"password": "new-pw"}, headers=ADMIN)
    assert client.get("/api/workspaces", headers=W).status_code == 401  # old password no longer works
    assert AGENT_PW


def test_long_paths_on_read_are_client_errors(client, ws):
    long = "a" * 300 + ".md"
    assert client.get("/api/workspaces/demo/file", params={"path": long}, headers=AGENT).status_code in (400, 404)
    assert client.get(f"/api/workspaces/demo/raw/{long}", headers=AGENT).status_code in (400, 404)
    assert client.get("/api/workspaces/demo/tree", params={"path": long}, headers=AGENT).status_code in (400, 404)


def test_metadata_read_has_hard_deadline():
    import time as _t

    import pytest

    class Trickle:
        def read1(self, n):
            _t.sleep(0.2)
            return b"x"

    start = _t.monotonic()
    with pytest.raises(ValueError):
        oauth._read_limited(Trickle(), _t.monotonic() + 0.6)
    assert _t.monotonic() - start < 1.5


def test_metadata_fetch_concurrency_is_capped():
    import pytest
    held = 0
    while oauth._cimd_slots.acquire(blocking=False):
        held += 1
    try:
        with pytest.raises(ValueError, match="too many"):
            oauth._fetch_cimd("https://8.8.8.8/client.json")
    finally:
        for _ in range(held):
            oauth._cimd_slots.release()
