import re
import time
from urllib.parse import parse_qs, urlparse

from caw import oauth
from conftest import ADMIN, ADMIN_PW, AGENT_PW, call_tool, oauth_login, pkce_pair


def test_metadata(client):
    prm = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == "https://caw.test/mcp" and prm["authorization_servers"] == ["https://caw.test"]
    asm = client.get("/.well-known/oauth-authorization-server").json()
    assert asm["issuer"] == "https://caw.test"
    assert asm["code_challenge_methods_supported"] == ["S256"]
    assert asm["client_id_metadata_document_supported"] is True
    assert "none" in asm["token_endpoint_auth_methods_supported"]
    assert "offline_access" in asm["scopes_supported"]


def test_public_url_overrides_host(tmp_path):
    from fastapi.testclient import TestClient

    from caw.app import create_app
    from caw.config import Settings
    s = Settings.from_env(data_dir=str(tmp_path / "d"), admin_password="x")
    s.public_url = "https://public.example"
    with TestClient(create_app(s), base_url="http://internal:8080") as c:
        assert c.get("/.well-known/oauth-authorization-server").json()["issuer"] == "https://public.example"


def test_registration_validation(client):
    assert client.post("/oauth/register", json={"redirect_uris": []}).status_code == 400
    assert client.post("/oauth/register", json={"redirect_uris": ["javascript:alert(1)"]}).status_code == 400
    assert client.post("/oauth/register", json={"redirect_uris": ["http://evil.example/cb"]}).status_code == 400
    r = client.post("/oauth/register", json={"redirect_uris": ["https://ok.example/cb"],
                                             "token_endpoint_auth_method": "client_secret_post"}).json()
    assert r["client_secret"] and r["token_endpoint_auth_method"] == "client_secret_post"


def test_full_flow_and_scopes(client, ws):
    tok, cid = oauth_login(client, AGENT_PW, client_name="Claude")
    H = {"Authorization": "Bearer " + tok["access_token"]}
    text, err = call_tool(client, H, "write_file", {"workspace": "demo", "path": "o.md", "content": "x"})
    assert not err
    assert client.get("/api/workspaces/demo/history", headers=ADMIN).json()["commits"][0]["author"] == "Claude"
    # admin password narrowed to one workspace
    tok2, _ = oauth_login(client, ADMIN_PW, workspace="demo", client_name="ChatGPT")
    me = client.get("/api/me", headers={"Authorization": "Bearer " + tok2["access_token"]}).json()
    assert me["role"] == "workspace" and me["workspace"] == "demo"
    # workspace password
    tok3, _ = oauth_login(client, "ws-pw", workspace="demo", client_name="Gemini")
    me = client.get("/api/me", headers={"Authorization": "Bearer " + tok3["access_token"]}).json()
    assert me["role"] == "workspace"
    conns = client.get("/api/connections", headers=ADMIN).json()["connections"]
    assert {c["client_name"] for c in conns} == {"Claude", "ChatGPT", "Gemini"}


def _start(client, redirect="https://app.example/cb", challenge=None):
    reg = client.post("/oauth/register", json={"client_name": "X", "redirect_uris": [redirect],
                                               "token_endpoint_auth_method": "none"}).json()
    verifier, ch = pkce_pair()
    page = client.get("/oauth/authorize", params={"response_type": "code", "client_id": reg["client_id"],
                                                  "redirect_uri": redirect, "state": "s",
                                                  "code_challenge": challenge or ch, "code_challenge_method": "S256"})
    rid = re.search(r'name="request_id" value="([^"]+)"', page.text).group(1)
    return reg, verifier, rid


def test_wrong_password_deny_and_bad_verifier(client):
    reg, verifier, rid = _start(client)
    r = client.post("/oauth/authorize", data={"request_id": rid, "password": "nope", "action": "approve"})
    assert r.status_code == 200 and "Invalid credentials" in r.text
    r = client.post("/oauth/authorize", data={"request_id": rid, "action": "deny"}, follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["error"] == ["access_denied"] and q["iss"] == ["https://caw.test"]
    reg, verifier, rid = _start(client)
    r = client.post("/oauth/authorize", data={"request_id": rid, "password": AGENT_PW, "action": "approve"},
                    follow_redirects=False)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    bad = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                            "client_id": reg["client_id"], "code_verifier": "wrong" * 10})
    assert bad.json()["error"] == "invalid_grant"
    # the code was consumed by the failed attempt
    again = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                              "client_id": reg["client_id"], "code_verifier": verifier})
    assert again.json()["error"] == "invalid_grant"


def test_pkce_required_and_redirect_checks(client):
    reg = client.post("/oauth/register", json={"redirect_uris": ["https://app.example/cb"]}).json()
    r = client.get("/oauth/authorize", params={"response_type": "code", "client_id": reg["client_id"],
                                               "redirect_uri": "https://app.example/cb"}, follow_redirects=False)
    assert r.status_code == 400 and "PKCE" in r.text  # error page, never a pre-consent redirect
    r = client.get("/oauth/authorize", params={"response_type": "token", "client_id": reg["client_id"],
                                               "redirect_uri": "https://app.example/cb"}, follow_redirects=False)
    assert r.status_code == 400 and "location" not in r.headers
    r = client.get("/oauth/authorize", params={"response_type": "code", "client_id": reg["client_id"],
                                               "redirect_uri": "https://evil.example/cb", "code_challenge": "x",
                                               "code_challenge_method": "S256"})
    assert r.status_code == 400 and "does not match" in r.text
    assert client.get("/oauth/authorize", params={"client_id": "nope"}).status_code == 400


def test_loopback_any_port(client):
    reg = client.post("/oauth/register", json={"redirect_uris": ["http://localhost/callback"]}).json()
    _, ch = pkce_pair()
    r = client.get("/oauth/authorize", params={"response_type": "code", "client_id": reg["client_id"],
                                               "redirect_uri": "http://localhost:51234/callback",
                                               "code_challenge": ch, "code_challenge_method": "S256"})
    assert r.status_code == 200 and "local computer" in r.text


def test_refresh_rotation(client, ws):
    tok, cid = oauth_login(client, AGENT_PW)
    t2 = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                           "client_id": cid}).json()
    assert t2["refresh_token"] != tok["refresh_token"]
    # within the grace period the old token still works (concurrent refresh)
    assert "access_token" in client.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": cid}).json()
    # after the grace period it does not
    db = client.app.state.ctx.db
    db.execute("UPDATE oauth_tokens SET replaced_at = ?, expires_at = ? WHERE replaced_at IS NOT NULL",
               (time.time() - 1000, time.time() + 1000))
    r = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                          "client_id": cid})
    assert r.json()["error"] == "invalid_grant"
    other = client.post("/oauth/register", json={"redirect_uris": ["https://x.example/cb"],
                                                 "token_endpoint_auth_method": "none"}).json()
    r = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": t2["refresh_token"],
                                          "client_id": other["client_id"]})
    assert r.json()["error"] == "invalid_grant"


def test_revoke(client, ws):
    tok, _ = oauth_login(client, AGENT_PW)
    H = {"Authorization": "Bearer " + tok["access_token"]}
    assert client.get("/api/workspaces", headers=H).status_code == 200
    client.post("/oauth/revoke", data={"token": tok["refresh_token"]})
    assert client.get("/api/workspaces", headers=H).status_code == 401


def test_cimd_client(client, monkeypatch):
    url = "https://claude.ai/oauth/claude-code-client-metadata"
    monkeypatch.setattr(oauth, "_fetch_cimd", lambda cid: {
        "client_id": cid, "client_name": "Claude Code", "redirect_uris": ["http://localhost/callback"]})
    verifier, ch = pkce_pair()
    page = client.get("/oauth/authorize", params={"response_type": "code", "client_id": url,
                                                  "redirect_uri": "http://127.0.0.1:4000/callback".replace("127.0.0.1", "localhost"),
                                                  "state": "s", "code_challenge": ch, "code_challenge_method": "S256"})
    assert page.status_code == 200 and "Claude Code" in page.text
    rid = re.search(r'name="request_id" value="([^"]+)"', page.text).group(1)
    r = client.post("/oauth/authorize", data={"request_id": rid, "password": AGENT_PW, "action": "approve"},
                    follow_redirects=False)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    tok = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": url,
                                            "code_verifier": verifier}).json()
    assert tok["token_type"] == "Bearer"


def test_cimd_rejects_private_hosts():
    import pytest
    for bad in ["http://example.com/x", "https://127.0.0.1/x", "https://localhost/x", "https://user:pw@example.com/x"]:
        with pytest.raises(ValueError):
            oauth._fetch_cimd(bad)


def test_login_page_language(client):
    reg = client.post("/oauth/register", json={"client_name": "X", "redirect_uris": ["https://a.example/cb"]}).json()
    _, ch = pkce_pair()
    params = {"response_type": "code", "client_id": reg["client_id"], "redirect_uri": "https://a.example/cb",
              "code_challenge": ch, "code_challenge_method": "S256"}
    zh = client.get("/oauth/authorize", params=params, headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
    assert "授权访问" in zh.text and zh.headers["content-language"] == "zh-CN"
    en = client.get("/oauth/authorize", params=params, headers={"Accept-Language": "fr-FR,fr;q=0.9"})
    assert "Authorize access" in en.text
    assert oauth._pick_language("zh-TW") == "zh-CN"  # same primary language falls back
    assert oauth._pick_language("de;q=0.5, zh;q=0.9") == "zh-CN"
