"""The setup message an admin copies into a new Claude task, and the skill zip it downloads."""

import io
import json
import os
import re
import shutil
import subprocess
import urllib.request
import zipfile

import pytest

from conftest import ADMIN, AGENT, basic


def setup(client, ws="demo", **body):
    return client.post(f"/api/workspaces/{ws}/setup-message", json=body, headers=ADMIN)


def commands(message: str) -> str:
    return re.search(r"```bash\n(.*?)\n```", message, re.S).group(1)


def test_message_contains_a_workspace_token(client, ws):
    r = setup(client, agent="Claude Cowork")
    assert r.status_code == 201, r.text
    data = r.json()
    msg = data["message"]
    token = re.search(r"--token (ph_[A-Za-z0-9_-]+)", msg).group(1)
    assert "https://posthouse.test" in msg and '"demo"' in msg
    assert "install-hooks --workspace demo" in msg and "start demo" in msg
    assert "--agent 'Claude Cowork'" in msg
    assert "https://posthouse.test/skill/posthouse-skill.zip" in msg
    assert data["token_name"].startswith("demo · setup message") and data["expires_at"] is None
    # the token opens this workspace and nothing else
    bearer = {"Authorization": "Bearer " + token}
    assert client.get("/api/workspaces/demo/session", headers=bearer).status_code == 200
    client.post("/api/workspaces", json={"name": "other"}, headers=ADMIN)
    assert client.get("/api/workspaces/other/session", headers=bearer).status_code in (403, 404)
    assert client.post("/api/workspaces", json={"name": "x"}, headers=bearer).status_code == 403
    tokens = client.get("/api/tokens", headers=ADMIN).json()["tokens"]
    assert any(t["id"] == data["token_id"] and t["workspace"] == "demo" for t in tokens)


def test_only_admins_and_known_workspaces(client, ws):
    assert setup(client).status_code == 201
    assert client.post("/api/workspaces/demo/setup-message", json={}, headers=AGENT).status_code == 403
    assert setup(client, ws="missing").status_code == 404
    assert setup(client, expires_days=0).status_code == 422
    assert setup(client, expires_days=30).json()["expires_at"]


def test_language_and_quoting(client):
    client.post("/api/workspaces", json={"name": "论文项目"}, headers=ADMIN)
    zh = setup(client, ws="论文项目", lang="zh-CN").json()["message"]
    assert zh.startswith("请把这个任务接入我的 Posthouse 工作区「论文项目」")
    assert "install-hooks --workspace '论文项目'" in zh
    for lang in ("../../etc/passwd", "xx", "zh-CN/../en", None):
        msg = setup(client, ws="论文项目", lang=lang).json()["message"]
        assert msg.startswith("Please connect this task")


def test_skill_zip_is_public(client):
    r = client.get("/skill/posthouse-skill.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "sandbox" in r.headers["content-security-policy"]
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "posthouse/SKILL.md" in names and "posthouse/scripts/posthouse.py" in names
    assert not any("__pycache__" in n for n in names)


@pytest.mark.skipif(not (shutil.which("bash") and shutil.which("curl")), reason="needs bash and curl")
def test_pasted_commands_work_in_a_fresh_environment(server, tmp_path):
    """Run the message's commands exactly as Claude would, in an empty home folder."""
    req = urllib.request.Request(server + "/api/workspaces", method="POST", data=b'{"name":"fresh-task"}',
                                 headers={**basic("admin", "admin"), "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)
    req = urllib.request.Request(server + "/api/workspaces/fresh-task/setup-message", method="POST", data=b"{}",
                                 headers={**basic("admin", "admin"), "Content-Type": "application/json"})
    msg = json.loads(urllib.request.urlopen(req, timeout=10).read())["message"]
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("POSTHOUSE_", "XDG_", "CLAUDE_"))}
    env.update(HOME=str(home), NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    r = subprocess.run(["bash", "-e", "-c", commands(msg)], cwd=project, env=env, capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "# Workspace: fresh-task" in r.stdout
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert {"UserPromptSubmit", "Stop"} <= set(settings["hooks"])
    cfg = json.loads((home / ".config" / "posthouse" / "config.json").read_text())
    assert cfg["token"].startswith("ph_") and cfg["projects"][os.path.realpath(project)] == "fresh-task"


# ------------------------------------------------- server-level message (no workspace needed)
def test_all_workspaces_message_uses_an_agent_token(client):
    r = client.post("/api/setup-message", json={"lang": "en"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    data = r.json()
    msg = data["message"]
    assert data["role"] == "agent" and "(all workspaces)" in data["token_name"]
    assert msg.startswith("Please connect this task to my Posthouse server")
    assert "posthouse.py ls" in commands(msg) and "--workspace" not in commands(msg)
    assert "install-hooks --workspace NAME" in msg and "create NAME" in msg
    token = re.search(r"--token (ph_[A-Za-z0-9_-]+)", msg).group(1)
    bearer = {"Authorization": "Bearer " + token}
    assert client.post("/api/workspaces", json={"name": "made-by-claude"}, headers=bearer).status_code == 201
    assert [w["name"] for w in client.get("/api/workspaces", headers=bearer).json()["workspaces"]] == ["made-by-claude"]
    assert client.post("/api/setup-message", json={}, headers=AGENT).status_code == 403
    zh = client.post("/api/setup-message", json={"lang": "zh-CN"}, headers=ADMIN).json()["message"]
    assert zh.startswith("请把这个任务接入我的 Posthouse 服务器")


@pytest.mark.skipif(not (shutil.which("bash") and shutil.which("curl")), reason="needs bash and curl")
def test_all_workspaces_message_works_on_an_empty_server(server, tmp_path):
    req = urllib.request.Request(server + "/api/setup-message", method="POST", data=b"{}",
                                 headers={**basic("admin", "admin"), "Content-Type": "application/json"})
    msg = json.loads(urllib.request.urlopen(req, timeout=10).read())["message"]
    home, project = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    project.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("POSTHOUSE_", "XDG_", "CLAUDE_"))}
    env.update(HOME=str(home), NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    run = lambda script: subprocess.run(["bash", "-e", "-c", script], cwd=project, env=env,  # noqa: E731
                                        capture_output=True, text=True, timeout=120)
    r = run(commands(msg))
    assert r.returncode == 0, r.stderr + r.stdout
    # what the message asks Claude to do next, once the user names the project
    cli = "python3 ~/.claude/skills/posthouse/scripts/posthouse.py"
    r = run(f"{cli} create thesis -d 'My thesis' && {cli} install-hooks --workspace thesis && {cli} start thesis")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "# Workspace: thesis" in r.stdout
    cfg = json.loads((home / ".config" / "posthouse" / "config.json").read_text())
    assert cfg["projects"][os.path.realpath(project)] == "thesis"
