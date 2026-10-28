"""Saving Claude Code conversations to a workspace: rendering, redaction, the hook and its installer."""

import io
import json
import os
import socket
import stat
import sys
import time
import urllib.parse
import urllib.error
import urllib.request

import pytest

from conftest import ADMIN, AGENT, basic, load_cli

SESSION = "0123abcd-1111-2222-3333-444455556666"
LIVE = basic("agent", "agent")  # the test_cli server


def row(kind, content, **extra):
    return {"type": kind, "timestamp": "2026-10-27T14:18:00.000Z", "sessionId": SESSION,
            "message": {"role": kind, "content": content}, **extra}


def write_transcript(path, rows, partial_tail=False):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        if partial_tail:
            f.write('{"type": "assistant", "message": {"content": [{"type": "te')


BASE_ROWS = [
    row("user", "This session is being continued.\n<system-reminder>internal</system-reminder>\nSummary: X",
        isCompactSummary=True),
    row("user", "Please deploy it.\n<system-reminder>hidden context</system-reminder>"),
    row("user", [{"type": "text", "text": "Base directory for this skill: ..."}], isMeta=True),
    row("assistant", [{"type": "thinking", "thinking": "secret plan", "signature": "abc"},
                      {"type": "text", "text": "Deploying now."},
                      {"type": "tool_use", "id": "t1", "name": "Bash",
                       "input": {"command": "GH_TOKEN=ghp_" + "a" * 36 + " ./deploy.sh", "description": "Deploy"}}]),
    row("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "ok\n" + "x" * 5000}]),
    row("assistant", [{"type": "text", "text": "subagent chatter"}], isSidechain=True),
    row("assistant", [{"type": "text", "text": "Deployed. Use ```code``` blocks."}]),
]


@pytest.fixture
def cli():
    return load_cli()


# ----------------------------------------------------------------- rendering
def test_entries_skip_internal_content(cli, tmp_path):
    t = tmp_path / "s.jsonl"
    write_transcript(t, BASE_ROWS, partial_tail=True)
    entries = cli.transcript_entries(str(t))
    md = "\n".join(e["md"] for e in entries)
    assert "## Summary of the earlier conversation" in md and "Summary: X" in md
    assert "Please deploy it." in md
    for hidden in ("internal", "hidden context", "Base directory", "secret plan", "subagent chatter"):
        assert hidden not in md
    assert "**Bash** — Deploy" in md
    assert "more characters]" in md  # long tool output is shortened
    assert [e["role"] for e in entries][-1] == "assistant"


def test_render_redacts_and_keeps_earlier_parts_stable(cli, tmp_path):
    t = tmp_path / "s.jsonl"
    rows = BASE_ROWS + [row("user", f"message {i} " + "y" * 300) for i in range(40)]
    write_transcript(t, rows)
    entries = cli.transcript_entries(str(t))
    parts = cli.render_transcript(entries, {"session": SESSION, "agent": "Claude Code"}, part_chars=3000, exact=[])
    assert len(parts) > 3
    text = "\n".join(parts)
    assert "ghp_" not in text and "GH_TOKEN=[REDACTED-GITHUB-TOKEN]" in text
    assert parts[0].startswith("# Conversation transcript\n") and "Claude Code" in parts[0]
    assert "continued from `part-001.md`" in parts[1]
    grown = cli.render_transcript(entries + entries[-3:], {"session": SESSION, "agent": "Claude Code"},
                                  part_chars=3000, exact=[])
    assert grown[:len(parts) - 1] == parts[:-1]
    # a message containing ``` cannot break out of its code block
    assert "````" in "\n".join(cli.render_transcript(
        [{"role": "result", "md": cli._code("a ``` b") + "\n"}], {}, exact=[]))


KEY = "PRIVATE" + " KEY-----"  # split so secret scanners don't flag the test data
REDACT_CASES = [
    ("ghp_" + "A" * 36, None),
    ("github_pat_" + "B" * 40, None),
    ("ph_" + "c" * 30, None),
    ("sk-ant-api03-" + "d" * 40, None),
    ("AKIA" + "E" * 16, None),
    ("xoxb-1234567890-abcdef", None),
    ("Authorization: Bearer abc.def.ghi", "Authorization: Bearer "),
    ("https://user:hunter22@example.com/x", "https://user:"),
    ("curl -u agent:s3cretpw http://x", "curl -u agent:"),
    ("POSTHOUSE_PASSWORD=s3cretpw", "POSTHOUSE_PASSWORD="),
    ("export API_KEY='abc123456'", "export API_KEY="),
    ("--password hunter22", "--password "),
    ("-----BEGIN OPENSSH " + KEY + "\nAAAA\n-----END OPENSSH " + KEY, None),
]


@pytest.mark.parametrize("secret, kept", REDACT_CASES, ids=[f"case{i}" for i in range(len(REDACT_CASES))])
def test_redact_patterns(cli, secret, kept):
    out = cli.redact(f"before {secret} after", [])
    assert "REDACTED" in out and out.startswith("before ") and out.endswith(" after")
    if kept:
        assert kept in out
    for piece in ("A" * 36, "B" * 40, "c" * 30, "hunter22", "s3cretpw", "abc123456", "AAAA", "abc.def.ghi"):
        assert piece not in out


@pytest.mark.parametrize("text", ['curl -u "$POSTHOUSE_USER:$POSTHOUSE_PASSWORD"', "POSTHOUSE_PASSWORD=$PW",
                                  "password = a.ws_password", "the token endpoint", "POSTHOUSE_ADMIN_PASSWORD="])
def test_redact_leaves_harmless_text(cli, text):
    assert cli.redact(text, []) == text


def test_exact_secret_values_from_environment(cli, monkeypatch, isolated):
    monkeypatch.setenv("MY_SERVICE_TOKEN", "plain-looking-value-123")
    assert cli.redact("x plain-looking-value-123 y", cli.known_secrets()) == "x [REDACTED] y"


# ---------------------------------------------------------------- the hook
def hook(cli, monkeypatch, capsys, payload):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode())))
    code = cli.main(["hook"])
    out = capsys.readouterr()
    return code, out.out


def get_file(server, ws, path):
    url = f"{server}/api/workspaces/{ws}/raw/" + urllib.parse.quote(path)
    req = urllib.request.Request(url, headers=LIVE)
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode()


def api(server, method, path, body=None, headers=LIVE):
    req = urllib.request.Request(server + path, method=method, headers={**headers, "Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def log_lines(home):
    return (home / ".cache" / "posthouse" / "hook.log").read_text().strip().splitlines()


def test_hook_saves_before_and_after_each_turn(cli, server, isolated, monkeypatch, capsys, tmp_path):
    home = isolated
    monkeypatch.setenv("POSTHOUSE_HOOK_SYNC", "1")  # upload inline so the test can check the server right away
    api(server, "POST", "/api/workspaces", {"name": "hooked"})
    assert cli.main(["--url", server, "--user", "agent", "--password", "agent", "--agent", "Claude Code", "login"]) == 0
    cfg_file = home / ".config" / "posthouse" / "config.json"
    assert stat.S_IMODE(cfg_file.stat().st_mode) == 0o600
    assert cli.main(["use", "hooked"]) == 0
    capsys.readouterr()

    t = tmp_path / "session.jsonl"
    write_transcript(t, BASE_ROWS)
    common = {"session_id": SESSION, "transcript_path": str(t), "cwd": os.getcwd()}

    # before the turn: the new prompt is not in the transcript yet, so it is added
    code, out = hook(cli, monkeypatch, capsys, {**common, "hook_event_name": "UserPromptSubmit",
                                                 "prompt": "Now write the release notes"})
    assert code == 0 and out == ""  # stdout would become part of Claude's context
    part = get_file(server, "hooked", "transcripts/2026-10-27_0123abcd/part-001.md")
    assert "Now write the release notes" in part and "(turn starting)" in part
    assert "ghp_" not in part and "Agent: Claude Code" in part
    assert (home / ".cache" / "posthouse" / "transcripts" / "2026-10-27_0123abcd" / "part-001.md").exists()

    # after the turn: the transcript has the turn, plus a final message not yet flushed to disk
    write_transcript(t, BASE_ROWS + [row("user", "Now write the release notes"),
                                     row("assistant", [{"type": "text", "text": "Drafting."}])])
    code, out = hook(cli, monkeypatch, capsys, {**common, "hook_event_name": "Stop",
                                                 "last_assistant_message": "Release notes are in files/NOTES.md."})
    assert code == 0 and out == ""
    part = get_file(server, "hooked", "transcripts/2026-10-27_0123abcd/part-001.md")
    assert "(turn starting)" not in part and part.count("Now write the release notes") == 1
    assert part.rstrip().endswith("Release notes are in files/NOTES.md.")

    # nothing changed: no upload, no new commit
    commits = api(server, "GET", "/api/workspaces/hooked/history?limit=50")["commits"]
    code, _ = hook(cli, monkeypatch, capsys, {**common, "hook_event_name": "Stop",
                                               "last_assistant_message": "Release notes are in files/NOTES.md."})
    assert api(server, "GET", "/api/workspaces/hooked/history?limit=50")["commits"] == commits
    assert log_lines(home)[-1].endswith("uploaded=0")
    assert [c["author"] for c in commits[:2]] == ["Claude Code", "Claude Code"]
    assert commits[0]["message"].startswith("Save transcript (after turn)")

    # the session bundle mentions the saved conversation but keeps it out of files and recent changes
    bundle = api(server, "GET", "/api/workspaces/hooked/session")
    assert bundle["transcripts"] == {"count": 1, "latest": ["transcripts/2026-10-27_0123abcd/"]}
    assert "transcripts/" in bundle["files"] and not any(f.startswith("transcripts/2026") for f in bundle["files"])
    assert not any(c["message"].startswith("Save transcript") for c in bundle["recent_changes"])
    md = urllib.request.urlopen(urllib.request.Request(
        server + "/api/workspaces/hooked/session?format=md", headers=LIVE), timeout=10).read().decode()
    assert "## Saved conversations" in md


def test_hook_never_blocks(cli, isolated, monkeypatch, capsys, tmp_path):
    home = isolated
    monkeypatch.setenv("POSTHOUSE_HOOK_SYNC", "1")
    t = tmp_path / "s.jsonl"
    write_transcript(t, BASE_ROWS)
    # no workspace linked: saved locally only
    code, out = hook(cli, monkeypatch, capsys, {"session_id": SESSION, "transcript_path": str(t),
                                                 "hook_event_name": "Stop", "cwd": os.getcwd()})
    assert code == 0 and out == "" and "locally only (no workspace linked)" in log_lines(home)[-1]
    # unreachable server: still exit 0, no output, quick, error logged
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    monkeypatch.setenv("POSTHOUSE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("POSTHOUSE_TOKEN", "ph_" + "z" * 30)
    monkeypatch.setenv("POSTHOUSE_WORKSPACE", "nowhere")
    started = time.time()
    code, out = hook(cli, monkeypatch, capsys, {"session_id": SESSION, "transcript_path": str(t),
                                                 "hook_event_name": "UserPromptSubmit", "prompt": "hi"})
    assert code == 0 and out == "" and time.time() - started < 10
    assert "error" in log_lines(home)[-1]
    # garbage on stdin
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"not json")))
    assert cli.main(["hook"]) == 0 and capsys.readouterr().out == ""


def test_save_transcript_command(cli, server, isolated, monkeypatch, capsys, tmp_path):
    api(server, "POST", "/api/workspaces", {"name": "manual"})
    projects = isolated / ".claude" / "projects" / "-proj"
    projects.mkdir(parents=True)
    write_transcript(projects / f"{SESSION}.jsonl", BASE_ROWS)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    assert cli.main(["save-transcript", "-o", str(tmp_path / "out.md")]) == 0
    assert "Deployed." in (tmp_path / "out.md").read_text()
    args = ["--url", server, "--user", "agent", "--password", "agent"]
    assert cli.main([*args, "save-transcript", "-w", "manual"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["uploaded"] == 1 and result["folder"] == "transcripts/2026-10-27_0123abcd"
    assert "Deployed." in get_file(server, "manual", "transcripts/2026-10-27_0123abcd/part-001.md")


def test_start_links_the_folder(cli, server, isolated, capsys):
    api(server, "POST", "/api/workspaces", {"name": "linked"})
    args = ["--url", server, "--user", "agent", "--password", "agent"]
    assert cli.main([*args, "start", "linked"]) == 0
    assert cli.main(["use"]) == 0
    assert capsys.readouterr().out.strip().endswith("linked")
    api(server, "POST", "/api/workspaces", {"name": "other"})
    cli.main([*args, "start", "other"])  # an existing link is kept
    capsys.readouterr()
    cli.main(["use"])
    assert capsys.readouterr().out.strip() == "linked"


# --------------------------------------------------------------- installer
def test_install_and_uninstall_hooks(cli, isolated, monkeypatch, capsys):
    settings = isolated / ".claude" / "settings.json"
    settings.parent.mkdir()
    other = {"type": "command", "command": "echo other"}
    settings.write_text(json.dumps({"model": "opus", "hooks": {"Stop": [{"hooks": [other]}]}}))
    monkeypatch.setenv("POSTHOUSE_URL", "https://posthouse.example")
    monkeypatch.setenv("POSTHOUSE_TOKEN", "ph_" + "k" * 30)
    assert cli.main(["install-hooks", "--workspace", "proj"]) == 0
    assert cli.main(["install-hooks"]) == 0  # idempotent
    data = json.loads(settings.read_text())
    assert data["model"] == "opus"
    ours = [h for g in data["hooks"]["Stop"] for h in g["hooks"] if "posthouse.py" in h["command"]]
    assert len(ours) == 1 and ours[0]["command"].endswith(" hook") and ours[0]["timeout"] == 30
    assert other in [h for g in data["hooks"]["Stop"] for h in g["hooks"]]
    assert len(data["hooks"]["UserPromptSubmit"]) == 1
    # credentials from the environment were saved for the hook (Claude Code starts hooks with its own env)
    cfg = json.loads((isolated / ".config" / "posthouse" / "config.json").read_text())
    assert cfg["url"] == "https://posthouse.example" and cfg["token"].startswith("ph_")
    assert cfg["projects"][os.path.realpath(os.getcwd())] == "proj"
    out = capsys.readouterr().out
    assert "before each turn" in out and "workspace: proj" in out

    assert cli.main(["uninstall-hooks"]) == 0
    data = json.loads(settings.read_text())
    assert data["hooks"] == {"Stop": [{"hooks": [other]}]}


def test_install_refuses_broken_settings(cli, isolated):
    settings = isolated / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text("{ not json")
    with pytest.raises(SystemExit):
        cli.main(["install-hooks", "--scope", "user"])
    assert settings.read_text() == "{ not json"


def test_local_scope_uses_settings_local_file(cli, isolated):
    assert cli.main(["install-hooks", "--scope", "local"]) == 0
    assert "UserPromptSubmit" in json.loads(open(".claude/settings.local.json").read())["hooks"]


# ----------------------------------------------------------- server behaviour
def test_identical_write_is_a_no_op(client, ws):
    client.put("/api/workspaces/demo/raw/a.md", content=b"same", headers=AGENT)
    before = client.get("/api/workspaces/demo/history", headers=AGENT).json()["commits"]
    acts = len(client.get("/api/activity?workspace=demo", headers=ADMIN).json()["activity"])
    r = client.put("/api/workspaces/demo/raw/a.md", content=b"same", headers=AGENT).json()
    assert r["unchanged"] is True
    assert client.get("/api/workspaces/demo/history", headers=AGENT).json()["commits"] == before
    assert len(client.get("/api/activity?workspace=demo", headers=ADMIN).json()["activity"]) == acts


def test_global_options_work_after_the_command(cli, server, isolated, capsys):
    """`login --url … --token …` (as documented) and `--url … login` both work."""
    assert cli.main(["login", "--url", server, "--user", "agent", "--password", "agent"]) == 0
    assert cli.main(["--url", server, "--user", "agent", "--password", "agent", "login"]) == 0
    assert cli.main(["ls", "--url", server, "--user", "agent", "--password", "agent"]) == 0
    capsys.readouterr()
    # the log command's own --agent-name still works next to the global --agent
    api(server, "POST", "/api/workspaces", {"name": "opts"})
    assert cli.main(["log", "opts", "--summary", "s", "--agent-name", "Gemini", "--agent", "x"]) == 0


def test_hook_uploads_in_the_background(cli, server, isolated, monkeypatch, capsys, tmp_path):
    """Default mode: the hook only saves locally and returns; a detached process uploads."""
    home = isolated
    api(server, "POST", "/api/workspaces", {"name": "bg"})
    assert cli.main(["--url", server, "--user", "agent", "--password", "agent", "login"]) == 0
    assert cli.main(["use", "bg"]) == 0
    capsys.readouterr()
    t = tmp_path / "session.jsonl"
    write_transcript(t, BASE_ROWS)
    started = time.time()
    code, out = hook(cli, monkeypatch, capsys, {"session_id": SESSION, "transcript_path": str(t),
                                                 "hook_event_name": "Stop", "cwd": os.getcwd()})
    assert code == 0 and out == "" and time.time() - started < 3
    assert "uploading to bg in the background" in log_lines(home)[-1]
    part = "transcripts/2026-10-27_0123abcd/part-001.md"
    for _ in range(100):  # wait for the background upload
        try:
            if "Deployed." in get_file(server, "bg", part):
                break
        except urllib.error.HTTPError:
            pass
        time.sleep(0.1)
    assert "Deployed." in get_file(server, "bg", part)
    for _ in range(50):
        if any(line.startswith(tuple("0123456789")) and " upload " in line for line in log_lines(home)):
            break
        time.sleep(0.1)
    assert any(" upload 0123abcd bg:" in line for line in log_lines(home))
