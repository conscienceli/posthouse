"""End-to-end: run the real server in a thread and drive it with skills/.../posthouse.py."""

import pytest

from conftest import load_cli


@pytest.fixture
def run(server, monkeypatch, capsys, isolated):
    cli = load_cli()
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    def _run(*args, stdin=None, user="agent", password="agent"):
        if stdin is not None:
            import io
            import sys
            monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin.encode())))
        code = cli.main(["--url", server, "--user", user, "--password", password, "--agent", "pytest", *args])
        out = capsys.readouterr()
        return code, out.out, out.err

    return _run


def test_cli_roundtrip(run, tmp_path):
    code, out, _ = run("create", "cli-ws", "-d", "from cli", "--workspace-password", "wpw")
    assert code == 0 and '"cli-ws"' in out
    code, out, _ = run("start", "cli-ws")
    assert out.startswith("# Workspace: cli-ws")
    code, out, _ = run("write", "cli-ws", "STATUS.md", stdin="# Status\nok\n")
    assert code == 0
    code, out, err = run("read", "cli-ws", "STATUS.md")
    version = err.split("version:")[1].strip()
    assert out == "# Status\nok\n"
    code, _, err = run("write", "cli-ws", "STATUS.md", "--expect", "0" * 16, stdin="x")
    assert code == 3 and "current version" in err
    code, _, _ = run("edit", "cli-ws", "STATUS.md", "--old", "ok", "--new", "done", "--expect", version)
    assert code == 0
    run("append", "cli-ws", "log.md", stdin="line\n")
    code, out, _ = run("search", "done", "-w", "cli-ws")
    assert "cli-ws/STATUS.md" in out
    code, out, _ = run("log", "cli-ws", "--summary", "Tested", "--next", "More")
    assert "journal/" in out
    code, out, _ = run("history", "cli-ws", "-n", "2")
    assert "pytest" in out
    code, out, _ = run("tree", "cli-ws", "-r")
    assert "log.md" in out
    run("mv", "cli-ws", "log.md", "logs/log.md")
    run("rm", "cli-ws", "logs", "-r")
    local = tmp_path / "f.bin"
    local.write_bytes(bytes(range(20)))
    code, out, _ = run("push", "cli-ws", str(local), "bin/f.bin")
    assert code == 0
    code, out, _ = run("pull", "cli-ws", str(tmp_path / "pulled"))
    assert (tmp_path / "pulled" / "cli-ws" / "bin" / "f.bin").read_bytes() == bytes(range(20))
    # workspace password only sees its workspace
    code, out, _ = run("ls", user="cli-ws", password="wpw")
    assert out.startswith("cli-ws")
    code, _, err = run("ls", password="wrong")
    assert code == 1 and "401" in err
