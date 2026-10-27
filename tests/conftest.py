import base64
import importlib.util
import socket
import threading
import time
from pathlib import Path
import hashlib
import re
import secrets
from urllib.parse import parse_qs, urlparse

import pytest
import uvicorn
from fastapi.testclient import TestClient

from posthouse.app import create_app
from posthouse.config import Settings

ADMIN_PW = "admin-pw-123"
AGENT_PW = "agent-pw-456"
BASE = "https://posthouse.test"


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


# ------------------------------------------------ live server + CLI (test_cli, test_transcripts)
POSTHOUSE_PY = Path(__file__).resolve().parents[1] / "skills" / "posthouse" / "scripts" / "posthouse.py"


def load_cli():
    spec = importlib.util.spec_from_file_location("posthouse_cli", POSTHOUSE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    data = tmp_path_factory.mktemp("cli")
    settings = Settings.from_env(data_dir=str(data), admin_password="admin", agent_password="agent")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    th.join(timeout=5)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Keep the CLI away from the real home: config, cache, Claude settings and transcripts."""
    for k in ("POSTHOUSE_URL", "POSTHOUSE_USER", "POSTHOUSE_PASSWORD", "POSTHOUSE_TOKEN", "POSTHOUSE_AGENT",
              "POSTHOUSE_WORKSPACE", "CLAUDE_PROJECT_DIR", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(k, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("POSTHOUSE_CONFIG", str(home / ".config" / "posthouse" / "config.json"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    return home
