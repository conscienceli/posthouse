import base64
import hashlib
import re
import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from caw.app import create_app
from caw.config import Settings

ADMIN_PW = "admin-pw-123"
AGENT_PW = "agent-pw-456"
BASE = "https://caw.test"


def basic(user: str, password: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


ADMIN = basic("admin", ADMIN_PW)
AGENT = basic("agent", AGENT_PW)


@pytest.fixture
def settings(tmp_path):
    return Settings.from_env(data_dir=str(tmp_path / "data"), admin_password=ADMIN_PW, agent_password=AGENT_PW)


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app, base_url=BASE) as c:
        yield c


@pytest.fixture
def ws(client):
    r = client.post("/api/workspaces", json={"name": "demo", "description": "Demo", "password": "ws-pw"},
                    headers=AGENT)
    assert r.status_code == 201, r.text
    return r.json()


def pkce_pair():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def oauth_login(client, password, workspace="", client_name="Test App",
                redirect_uri="https://app.example/callback"):
    """Run DCR + authorize + token exchange; returns (token_response, client_id)."""
    reg = client.post("/oauth/register", json={"client_name": client_name, "redirect_uris": [redirect_uri],
                                                "token_endpoint_auth_method": "none"}).json()
    verifier, challenge = pkce_pair()
    page = client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": reg["client_id"], "redirect_uri": redirect_uri, "state": "st",
        "code_challenge": challenge, "code_challenge_method": "S256"})
    assert page.status_code == 200, page.text
    rid = re.search(r'name="request_id" value="([^"]+)"', page.text).group(1)
    r = client.post("/oauth/authorize", data={"request_id": rid, "password": password, "workspace": workspace,
                                              "action": "approve"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    q = parse_qs(urlparse(r.headers["location"]).query)
    tok = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                            "redirect_uri": redirect_uri, "client_id": reg["client_id"],
                                            "code_verifier": verifier})
    assert tok.status_code == 200, tok.text
    return tok.json(), reg["client_id"]


def mcp(client, method, params=None, headers=None, msg_id=1, path="/mcp"):
    body = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(path, json=body, headers=headers or {})


def call_tool(client, headers, name, args=None, path="/mcp"):
    r = mcp(client, "tools/call", {"name": name, "arguments": args or {}}, headers=headers, path=path)
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    return res["content"][0]["text"], res["isError"]
