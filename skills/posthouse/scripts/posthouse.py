#!/usr/bin/env python3
"""posthouse.py - command-line client for a Posthouse server (Python 3.8+, stdlib only).

Credentials come from flags, then environment variables, then the config file written by
`posthouse.py login` (~/.config/posthouse/config.json, readable only by you):
  POSTHOUSE_URL       server base URL, e.g. https://posthouse.example.com
  POSTHOUSE_USER      'agent', 'admin' or a workspace name (for Basic auth with a password)
  POSTHOUSE_PASSWORD  the matching password
  POSTHOUSE_TOKEN     an access token (ph_...) instead of user/password
  POSTHOUSE_AGENT     your name, recorded as the author of changes (e.g. "Claude Code")

Examples:
  posthouse.py ls
  posthouse.py start my-project
  posthouse.py read my-project STATUS.md
  echo "# Status ..." | posthouse.py write my-project STATUS.md --expect 1a2b3c4d5e6f7a8b
  posthouse.py edit my-project STATUS.md --old "- [ ] draft" --new "- [x] draft"
  posthouse.py log my-project --summary "Drafted section 2" --next "Review section 3"

Save Claude Code conversations to a workspace automatically (before and after every turn):
  posthouse.py login --url https://posthouse.example.com --token ph_...
  posthouse.py install-hooks --workspace my-project
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import glob
import hashlib
import io
import json
import os
import re
import shlex
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

__version__ = "0.4.1"
EXIT_CONFLICT = 3
CRED_KEYS = ("url", "user", "password", "token", "agent")


# --------------------------------------------------------------------- config
def config_path() -> str:
    if os.environ.get("POSTHOUSE_CONFIG"):
        return os.path.expanduser(os.environ["POSTHOUSE_CONFIG"])
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "posthouse", "config.json")


def cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "posthouse")


def load_config() -> dict:
    try:
        with open(config_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> str:
    path = config_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def resolve_creds(a, cfg: dict | None = None) -> dict:
    """Flags and environment win; the config file fills in only when they carry no credential."""
    cfg = load_config() if cfg is None else cfg
    given = {k: getattr(a, k, None) or os.environ.get("POSTHOUSE_" + k.upper()) for k in CRED_KEYS}
    if not (given["password"] or given["token"]):
        for k in ("user", "password", "token"):
            given[k] = cfg.get(k)
    given["url"] = given["url"] or cfg.get("url")
    given["agent"] = given["agent"] or cfg.get("agent")
    return given


def project_dir(hook_input: dict | None = None) -> str:
    d = os.environ.get("CLAUDE_PROJECT_DIR") or (hook_input or {}).get("cwd") or os.getcwd()
    return os.path.realpath(d)


def bound_workspace(cfg: dict, directory: str, session_id: str | None = None) -> str | None:
    if session_id and (cfg.get("sessions") or {}).get(session_id):
        return cfg["sessions"][session_id]
    return (cfg.get("projects") or {}).get(os.path.realpath(directory))


def bind(directory: str, workspace: str, session_id: str | None = None) -> None:
    cfg = load_config()
    cfg.setdefault("projects", {})[os.path.realpath(directory)] = workspace
    if session_id:
        sessions = cfg.setdefault("sessions", {})
        sessions.pop(session_id, None)
        sessions[session_id] = workspace
        for old in list(sessions)[:-50]:  # keep the 50 most recent sessions
            del sessions[old]
    save_config(cfg)


class PosthouseError(Exception):
    def __init__(self, status: int, message: str, data: dict | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.data = data or {}


class Client:
    def __init__(self, url: str, user: str | None, password: str | None, token: str | None, agent: str | None,
                 timeout: float = 60):
        if not url:
            raise SystemExit("posthouse: no server URL. Set POSTHOUSE_URL, pass --url or run `posthouse.py login`.")
        self.base = url.rstrip("/")
        self.timeout = timeout
        self.headers = {"User-Agent": f"posthouse.py/{__version__}"}
        if token:
            self.headers["Authorization"] = "Bearer " + token
        elif password:
            raw = f"{user or 'agent'}:{password}".encode("utf-8")
            self.headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        if agent:
            self.headers["X-Posthouse-Agent"] = agent

    def request(self, method: str, path: str, *, query: dict | None = None, body=None, raw: bytes | None = None,
                headers: dict | None = None, want_raw: bool = False):
        url = self.base + path
        if query:
            q = {k: ("true" if v is True else "false" if v is False else v) for k, v in query.items() if v is not None}
            if q:
                url += "?" + urllib.parse.urlencode(q)
        h = dict(self.headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        elif raw is not None:
            data = raw
            h["Content-Type"] = "application/octet-stream"
        if headers:
            h.update(headers)
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = r.read()
                if want_raw:
                    return payload, dict(r.headers)
                ctype = r.headers.get("Content-Type", "")
                return json.loads(payload.decode("utf-8")) if "json" in ctype else payload.decode("utf-8")
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            try:
                info = json.loads(text)
            except ValueError:
                info = {"message": text or e.reason}
            raise PosthouseError(e.code, info.get("message") or str(info), info) from None
        except urllib.error.URLError as e:
            raise PosthouseError(0, f"cannot reach {self.base}: {e.reason}") from None


def ws_path(ws: str) -> str:
    return "/api/workspaces/" + urllib.parse.quote(ws, safe="")


def file_url(ws: str, path: str) -> str:
    return ws_path(ws) + "/raw/" + urllib.parse.quote(path.lstrip("/"), safe="/")


def read_input(src: str | None) -> bytes:
    if src in (None, "-"):
        return sys.stdin.buffer.read()
    with open(src, "rb") as f:
        return f.read()


def out(obj) -> None:
    if isinstance(obj, (dict, list)):
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(obj if obj.endswith("\n") else obj + "\n")


# ------------------------------------------------------------------- commands
def cmd_info(c: Client, a):
    info = c.request("GET", "/api/info")
    me = c.request("GET", "/api/me")
    out({"server": info, "me": me})


def cmd_ls(c: Client, a):
    data = c.request("GET", "/api/workspaces")
    if a.json:
        return out(data)
    rows = data["workspaces"]
    if not rows:
        print("(no workspaces)")
    for w in rows:
        lock = " [password]" if w.get("has_password") else ""
        print(f"{w['name']}{lock}\t{w['file_count']} files\tupdated {w['updated_at']}\t{w['description'][:80]}")


def cmd_create(c: Client, a):
    out(c.request("POST", "/api/workspaces", body={"name": a.name, "description": a.description or "",
                                                   "password": a.ws_password, "template": not a.empty}))


def cmd_start(c: Client, a):
    out(c.request("GET", ws_path(a.workspace) + "/session", query={"format": "md"}))
    try:  # lets the transcript hooks know where this project's conversations belong
        sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
        if not bound_workspace(load_config(), project_dir(), sid):
            bind(project_dir(), a.workspace, sid)
    except OSError:
        pass


def cmd_tree(c: Client, a):
    data = c.request("GET", ws_path(a.workspace) + "/tree", query={"path": a.path or "", "recursive": a.recursive})
    if a.json:
        return out(data)
    for e in data["entries"]:
        print(e["path"] + ("/" if e["type"] == "dir" else f"\t{e['size']}"))


def cmd_read(c: Client, a):
    if a.offset or a.limit or a.json:
        data = c.request("GET", ws_path(a.workspace) + "/file",
                         query={"path": a.path, "offset": a.offset, "limit": a.limit, "revision": a.rev})
        if a.json:
            return out(data)
        print(f"version: {data['version']}", file=sys.stderr)
        return out(data["content"])
    payload, headers = c.request("GET", file_url(a.workspace, a.path), query={"revision": a.rev}, want_raw=True)
    print(f"version: {headers.get('X-Posthouse-Version') or headers.get('x-posthouse-version')}", file=sys.stderr)
    sys.stdout.buffer.write(payload)
    if payload and not payload.endswith(b"\n"):
        sys.stdout.write("\n")


def cmd_write(c: Client, a):
    data = read_input(a.file)
    h = {}
    if a.expect:
        h["If-Match"] = a.expect
    if a.new:
        h["If-None-Match"] = "*"
    if a.message:
        h["X-Posthouse-Message"] = a.message
    out(c.request("PUT", file_url(a.workspace, a.path), raw=data, headers=h))


def cmd_append(c: Client, a):
    out(c.request("PUT", file_url(a.workspace, a.path), raw=read_input(a.file), query={"append": True}))


def _text_arg(text: str | None, path: str | None, name: str) -> str:
    if path:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    if text is None:
        raise SystemExit(f"posthouse: --{name} or --{name}-file is required")
    return text


def cmd_edit(c: Client, a):
    out(c.request("POST", ws_path(a.workspace) + "/edit", body={
        "path": a.path, "old_text": _text_arg(a.old, a.old_file, "old"), "new_text": _text_arg(a.new, a.new_file, "new"),
        "replace_all": a.all, "expected_version": a.expect}))


def cmd_mv(c: Client, a):
    out(c.request("POST", ws_path(a.workspace) + "/move",
                  body={"source": a.source, "destination": a.destination, "overwrite": a.overwrite}))


def cmd_rm(c: Client, a):
    out(c.request("DELETE", ws_path(a.workspace) + "/file", query={"path": a.path, "recursive": a.recursive}))


def cmd_mkdir(c: Client, a):
    out(c.request("POST", ws_path(a.workspace) + "/mkdir", body={"path": a.path}))


def cmd_search(c: Client, a):
    data = c.request("GET", "/api/search", query={"q": a.query, "workspace": a.workspace, "limit": a.limit})
    if a.json:
        return out(data)
    for r in data["results"]:
        print(r["id"])
        for m in r["matches"]:
            print(f"   {m['line']}: {m['text']}")


def cmd_log(c: Client, a):
    status = None
    if a.status_file:
        with open(a.status_file, "r", encoding="utf-8") as f:
            status = f.read()
    out(c.request("POST", ws_path(a.workspace) + "/progress", body={
        "summary": a.summary, "next_steps": a.next, "status": status, "agent": a.agent_name}))


def cmd_history(c: Client, a):
    data = c.request("GET", ws_path(a.workspace) + "/history", query={"path": a.path, "limit": a.n})
    if a.json:
        return out(data)
    for cm in data["commits"]:
        files = ", ".join(f"{f['status']} {f['path']}" for f in cm["files"][:6])
        print(f"{cm['short']}  {cm['time']}  {cm['author']}: {cm['message']}" + (f"  [{files}]" if files else ""))


def cmd_pull(c: Client, a):
    payload, _ = c.request("GET", ws_path(a.workspace) + "/archive", want_raw=True)
    dest = a.dir or "."
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        for m in z.namelist():
            target = os.path.realpath(os.path.join(dest, m))
            if not target.startswith(os.path.realpath(dest) + os.sep):
                raise SystemExit(f"posthouse: refusing unsafe path in archive: {m}")
        z.extractall(dest)
    print(f"extracted to {os.path.join(dest, a.workspace)}")


def cmd_push(c: Client, a):
    remote = a.remote or os.path.basename(a.local)
    with open(a.local, "rb") as f:
        data = f.read()
    out(c.request("PUT", file_url(a.workspace, remote), raw=data))


# ---------------------------------------------------------------- transcripts
TRANSCRIPTS_DIR = "transcripts"
PART_CHARS = 200_000   # one transcript part; only the last part changes as a conversation grows
RESULT_CHARS = 2_000   # kept from each tool result
TEXT_CHARS = 60_000    # kept from one message
REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>\s*", re.S)
SECRET_NAME_RE = re.compile(r"TOKEN|PASSWORD|PASSWD|SECRET|API_?KEY|ACCESS_KEY|CREDENTIAL|PRIVATE_KEY", re.I)
SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S),
     "[REDACTED-PRIVATE-KEY]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), "[REDACTED-GITHUB-TOKEN]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "[REDACTED-GITHUB-TOKEN]"),
    (re.compile(r"\bph_[A-Za-z0-9_-]{16,}"), "[REDACTED-POSTHOUSE-TOKEN]"),
    (re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"), "[REDACTED-API-KEY]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), "[REDACTED-API-KEY]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED-AWS-KEY]"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), "[REDACTED-SLACK-TOKEN]"),
    (re.compile(r"(?i)\b(authorization:\s*(?:bearer|basic|token)\s+)[^\s\"']+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b(bearer\s+)(?!\[)[A-Za-z0-9._~+/=-]{20,}"), r"\1[REDACTED]"),
    (re.compile(r"(://[^/\s:@]+:)(?![\[$])[^/\s@]+@"), r"\1[REDACTED]@"),
    (re.compile(r"(\s-u\s+['\"]?[^\s:'\"]+:)(?![\[$])[^\s'\"]+"), r"\1[REDACTED]"),
    (re.compile(r"\b((?:[A-Z][A-Z0-9_]*)?(?:TOKEN|PASSWORD|PASSWD|SECRET|API_KEY|APIKEY|ACCESS_KEY))(\s*[=:]\s*)"
                r"(?![\[$\s])(['\"]?)[^\s'\"]+\3"), r"\1\2[REDACTED]"),
    (re.compile(r"(--(?:password|token|secret|api-key|workspace-password)[= ])(?![\[$-])\S+"), r"\1[REDACTED]"),
]


def known_secrets() -> list:
    """Exact values to hide: secret-looking environment variables and the saved credentials."""
    vals = {v for k, v in os.environ.items() if v and len(v) >= 8 and SECRET_NAME_RE.search(k)}
    cfg = load_config()
    vals.update(cfg[k] for k in ("password", "token") if isinstance(cfg.get(k), str) and len(cfg[k]) >= 6)
    return sorted(vals, key=len, reverse=True)


def redact(text: str, exact: list | None = None) -> str:
    for v in exact or ():
        text = text.replace(v, "[REDACTED]")
    for pattern, repl in SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if limit and len(text) > limit:
        return text[:limit].rstrip() + f"\n… [{len(text) - limit:,} more characters]"
    return text


def _code(text: str, lang: str = "") -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{text}\n{fence}"


def _stamp(ts) -> str:
    if not ts:
        return ""
    try:
        t = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return str(ts)[:16]


def _now_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for c in content or []:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(c.get("text") or "")
        elif isinstance(c, dict) and c.get("type") == "image":
            parts.append("[image]")
    return "\n".join(parts)


def _tool_md(block: dict) -> str:
    name = block.get("name") or "tool"
    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
    if name == "Bash" and "command" in inp:
        desc = f" — {inp['description']}" if inp.get("description") else ""
        return f"**Bash**{desc}\n\n" + _code(_clip(str(inp["command"]), 4000), "bash")
    if name in ("Read", "Edit", "NotebookEdit") and inp.get("file_path"):
        return f"**{name}** `{inp['file_path']}`"
    if name == "Write" and inp.get("file_path"):
        body = str(inp.get("content", ""))
        return f"**Write** `{inp['file_path']}` ({len(body):,} characters)\n\n" + _code(_clip(body, RESULT_CHARS))
    if name in ("Grep", "Glob") and inp.get("pattern"):
        where = f" in `{inp['path']}`" if inp.get("path") else ""
        return f"**{name}** `{inp['pattern']}`{where}"
    return f"**{name}**\n\n" + _code(_clip(json.dumps(inp, ensure_ascii=False, indent=1), 1500), "json")


def transcript_entries(path: str | None, result_chars: int = RESULT_CHARS) -> list:
    """Readable pieces of a Claude Code transcript (JSONL): messages, tool calls and shortened results.

    Thinking, injected system context, tool lists and subagent chatter are left out.
    """
    entries = []
    if not path or not os.path.isfile(path):
        return entries
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue  # the last line may still be being written
            role = row.get("type")
            if role not in ("user", "assistant") or row.get("isSidechain") or row.get("isMeta"):
                continue
            content = (row.get("message") or {}).get("content")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if not isinstance(content, list):
                continue
            stamp = _stamp(row.get("timestamp"))
            for b in content:
                if not isinstance(b, dict):
                    continue
                kind = b.get("type")
                if kind == "text":
                    text = b.get("text") or ""
                    if role == "user":
                        text = REMINDER_RE.sub("", text)
                    text = text.strip()
                    if not text:
                        continue
                    title = ("Summary of the earlier conversation" if row.get("isCompactSummary")
                             else "User" if role == "user" else "Assistant")
                    entries.append({"role": role, "text": text,
                                    "md": f"## {title} · {stamp}\n\n{_clip(text, TEXT_CHARS)}\n"})
                elif kind == "image":
                    entries.append({"role": role, "md": "_[image]_\n"})
                elif kind == "tool_use":
                    entries.append({"role": "tool", "md": _tool_md(b) + "\n"})
                elif kind == "tool_result":
                    text = _clip(_result_text(b.get("content")), result_chars)
                    if text:
                        label = "Result (error)" if b.get("is_error") else "Result"
                        entries.append({"role": "result", "md": f"<details><summary>{label}</summary>\n\n"
                                                                f"{_code(text)}\n\n</details>\n"})
    return entries


def _first_timestamp(path: str | None) -> str | None:
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                ts = json.loads(line).get("timestamp")
            except ValueError:
                continue
            if ts:
                return ts
    return None


def render_transcript(entries: list, meta: dict, tail: str = "", part_chars: int = PART_CHARS,
                      exact: list | None = None) -> list:
    """Split the transcript into Markdown parts. Earlier parts stay byte-identical as it grows."""
    exact = known_secrets() if exact is None else exact
    chunks, cur, size = [], [], 0
    for e in entries:
        md = redact(e["md"], exact)
        if cur and size + len(md) > part_chars:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(md)
        size += len(md) + 1
    chunks.append(cur)
    if tail:
        chunks[-1].append(redact(tail, exact))
    parts = []
    for i, chunk in enumerate(chunks, 1):
        if i == 1:
            head = ("# Conversation transcript\n\n"
                    f"- Agent: {meta.get('agent') or 'unknown'}\n"
                    f"- Session: `{meta.get('session') or 'unknown'}`\n"
                    f"- Project folder: `{meta.get('project') or 'unknown'}`\n"
                    f"- Started: {_stamp(meta.get('started')) or 'unknown'}\n\n"
                    "Saved automatically before and after each turn by the Posthouse hook. Secrets are "
                    "redacted and long tool output is shortened. This is a raw record: STATUS.md and "
                    "journal/ hold the current state.\n")
        else:
            head = (f"# Conversation transcript, part {i}\n\n"
                    f"Session `{meta.get('session') or 'unknown'}`, continued from `part-{i - 1:03d}.md`.\n")
        parts.append(head + "\n" + "\n".join(chunk))
    return parts


def find_transcript(session_id: str | None = None) -> str | None:
    root = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    if session_id:
        hits = glob.glob(os.path.join(root, "*", glob.escape(session_id) + ".jsonl"))
        if hits:
            return max(hits, key=os.path.getmtime)
    hits = glob.glob(os.path.join(root, "*", "*.jsonl"))
    return max(hits, key=os.path.getmtime) if hits else None


class _StateLock:
    """Serialize hook runs for one session (UserPromptSubmit and Stop can overlap)."""

    def __init__(self, path: str, wait: float = 15):
        self.path, self.wait, self.f = path + ".lock", wait, None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.f = open(self.path, "a+")
        try:
            import fcntl
        except ImportError:  # Windows: best effort without a lock
            return self
        deadline = time.time() + self.wait
        while True:
            try:
                fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.time() > deadline:
                    raise TimeoutError("another save is still running")
                time.sleep(0.2)

    def __exit__(self, *exc):
        self.f.close()


def save_transcript(client: Client | None, workspace: str | None, transcript: str | None, session_id: str,
                    agent: str, project: str, tail: str = "", event: str = "manual",
                    entries: list | None = None) -> dict:
    """Save the conversation locally (cache) and upload the parts that changed to the workspace."""
    state_path = os.path.join(cache_dir(), "sessions", re.sub(r"[^A-Za-z0-9_.-]", "_", session_id) + ".json")
    with _StateLock(state_path):
        try:
            with open(state_path, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            state = {}
        started = state.get("started") or _first_timestamp(transcript) or dt.datetime.now(dt.timezone.utc).isoformat()
        folder = state.get("folder") or f"{TRANSCRIPTS_DIR}/{_stamp(started)[:10]}_{session_id[:8]}"
        meta = {"agent": agent, "session": session_id, "project": project, "started": started}
        parts = render_transcript(transcript_entries(transcript) if entries is None else entries, meta, tail=tail)
        local_dir = os.path.join(cache_dir(), folder)
        os.makedirs(local_dir, exist_ok=True)
        for i, text in enumerate(parts, 1):
            with open(os.path.join(local_dir, f"part-{i:03d}.md"), "w", encoding="utf-8") as f:
                f.write(text)
        target = f"{client.base if client else ''}|{workspace or ''}"
        if state.get("target") != target:
            state = {"target": target, "uploaded": {}}
        state.update({"started": started, "folder": folder})
        result = {"workspace": workspace, "folder": folder, "parts": len(parts), "uploaded": 0,
                  "local": local_dir}

        def keep():
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state, f)

        if client and workspace:
            label = {"UserPromptSubmit": "before turn", "Stop": "after turn"}.get(event, event)
            for i, text in enumerate(parts, 1):
                name = f"{folder}/part-{i:03d}.md"
                data = text.encode("utf-8")
                ver = hashlib.sha256(data).hexdigest()[:16]
                if state["uploaded"].get(name) == ver:
                    continue
                client.request("PUT", file_url(workspace, name), raw=data,
                               headers={"X-Posthouse-Message": f"Save transcript ({label}): {name}"})
                state["uploaded"][name] = ver
                result["uploaded"] += 1
                keep()
        keep()
        return result


def _hook_log(line: str) -> None:
    try:
        path = os.path.join(cache_dir(), "hook.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > 512_000:
            with open(path, encoding="utf-8", errors="replace") as f:
                keep = f.read()[-256_000:]
            with open(path, "w", encoding="utf-8") as f:
                f.write(keep)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {line}\n")
    except OSError:
        pass


def cmd_hook(a) -> int:
    """Claude Code hook: save the conversation before (UserPromptSubmit) and after (Stop) each turn.

    It never prints to stdout (on UserPromptSubmit that would be added to Claude's context)
    and always exits 0, so a slow or unreachable server never blocks the conversation.
    """
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            data = {}
    except (ValueError, OSError):
        data = {}
    event = data.get("hook_event_name") or a.event or "manual"
    try:
        cfg = load_config()
        creds = resolve_creds(a, cfg)
        session_id = (data.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID")
                      or os.path.splitext(os.path.basename(data.get("transcript_path") or "session"))[0])
        project = project_dir(data)
        ws = a.workspace or os.environ.get("POSTHOUSE_WORKSPACE") or bound_workspace(cfg, project, session_id)
        transcript = data.get("transcript_path") or find_transcript(session_id)
        tail = ""
        entries = transcript_entries(transcript)
        if event == "UserPromptSubmit" and (data.get("prompt") or "").strip():
            last_user = next((e.get("text") for e in reversed(entries) if e["role"] == "user"), None)
            if (last_user or "").strip() != data["prompt"].strip():
                tail = f"## User · {_now_stamp()} (turn starting)\n\n{_clip(data['prompt'], TEXT_CHARS)}\n"
        elif event == "Stop" and (data.get("last_assistant_message") or "").strip():
            last_ai = next((e.get("text") for e in reversed(entries) if e["role"] == "assistant"), None)
            if (last_ai or "").strip() != data["last_assistant_message"].strip():
                tail = f"## Assistant · {_now_stamp()}\n\n{_clip(data['last_assistant_message'], TEXT_CHARS)}\n"
        client = None
        if ws and creds["url"]:
            client = Client(creds["url"], creds["user"], creds["password"], creds["token"],
                            creds["agent"] or "Claude Code", timeout=8)
        r = save_transcript(client, ws, transcript, session_id, creds["agent"] or "Claude Code", project,
                            tail=tail, event=event, entries=entries)
        where = f"{ws}:{r['folder']}" if client else f"local only ({'no workspace linked' if not ws else 'no server URL'})"
        _hook_log(f"{event} {session_id[:8]} {where} parts={r['parts']} uploaded={r['uploaded']}")
    except BaseException as e:  # never let a hook failure reach the conversation
        _hook_log(f"{event} error: {type(e).__name__}: {e}")
    return 0


def cmd_save_transcript(a) -> int:
    transcript = a.transcript or find_transcript(os.environ.get("CLAUDE_CODE_SESSION_ID"))
    if not transcript:
        raise SystemExit("posthouse: no Claude Code transcript found; pass --transcript FILE.jsonl")
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.path.splitext(os.path.basename(transcript))[0]
    cfg = load_config()
    creds = resolve_creds(a, cfg)
    if a.output:
        meta = {"agent": creds["agent"] or "Claude Code", "session": session_id, "project": project_dir(),
                "started": _first_timestamp(transcript)}
        text = "\n".join(render_transcript(transcript_entries(transcript, a.max_result), meta, part_chars=10 ** 12))
        if a.output == "-":
            sys.stdout.write(text)
        else:
            with open(a.output, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"saved {len(text):,} characters to {a.output}", file=sys.stderr)
        return 0
    ws = a.workspace or os.environ.get("POSTHOUSE_WORKSPACE") or bound_workspace(cfg, project_dir(), session_id)
    if not ws:
        raise SystemExit("posthouse: which workspace? Pass --workspace or link this folder with `posthouse.py use WS`.")
    client = Client(creds["url"], creds["user"], creds["password"], creds["token"], creds["agent"])
    out(save_transcript(client, ws, transcript, session_id, creds["agent"] or "Claude Code", project_dir()))
    return 0


SETTINGS_FILES = {"user": os.path.join("~", ".claude", "settings.json"),
                  "project": os.path.join(".claude", "settings.json"),
                  "local": os.path.join(".claude", "settings.local.json")}
HOOK_EVENTS = ("UserPromptSubmit", "Stop")


def _settings_path(scope: str) -> str:
    return os.path.abspath(os.path.expanduser(SETTINGS_FILES[scope]))


def _is_our_hook(h) -> bool:
    cmd = h.get("command", "") if isinstance(h, dict) else ""
    return "posthouse.py" in cmd and cmd.rstrip().endswith(" hook")


def _edit_settings(scope: str, install: bool) -> str:
    path = _settings_path(scope)
    settings = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        try:
            settings = json.loads(text) if text.strip() else {}
        except ValueError:
            raise SystemExit(f"posthouse: {path} is not valid JSON; fix it first (nothing was changed).")
    hooks = settings.setdefault("hooks", {})
    command = f"{shlex.quote(sys.executable)} {shlex.quote(os.path.abspath(__file__))} hook"
    for event in HOOK_EVENTS:
        groups = [g for g in hooks.get(event, []) if isinstance(g, dict)]
        for g in groups:
            g["hooks"] = [h for h in g.get("hooks", []) if not _is_our_hook(h)]
        groups = [g for g in groups if g.get("hooks")]
        if install:
            groups.append({"hooks": [{"type": "command", "command": command, "timeout": 30}]})
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks", None)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".posthouse-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return path


def cmd_install_hooks(a) -> int:
    cfg = load_config()
    env_creds = {k: getattr(a, k, None) or os.environ.get("POSTHOUSE_" + k.upper()) for k in CRED_KEYS}
    if env_creds["url"] and (env_creds["password"] or env_creds["token"]) and not (cfg.get("password") or cfg.get("token")):
        # Claude Code starts hooks with its own environment, so the credentials must be in the config file.
        cfg.update({k: v for k, v in env_creds.items() if v})
        save_config(cfg)
        print(f"Saved the server URL and credentials to {config_path()} (readable only by you).")
    creds = resolve_creds(a, load_config())
    here = os.path.realpath(os.path.abspath(__file__))
    import tempfile
    if here.startswith(os.path.realpath(tempfile.gettempdir()) + os.sep):
        print(f"Warning: {here} is in a temporary folder and may disappear. Copy the skill somewhere permanent "
              "(for example ~/.claude/skills/posthouse/) and run install-hooks from there.")
    path = _edit_settings(a.scope, install=True)
    print(f"Installed hooks in {path}: save the conversation before each turn (UserPromptSubmit) "
          "and after each turn (Stop).")
    if a.workspace:
        bind(project_dir(), a.workspace, os.environ.get("CLAUDE_CODE_SESSION_ID"))
    ws = bound_workspace(load_config(), project_dir())
    print(f"This folder ({project_dir()}) saves to workspace: {ws or '(none yet)'}")
    if not ws:
        print("Link a workspace with `posthouse.py use WORKSPACE` (or `start WORKSPACE`) in each project folder.")
    if not creds["url"] or not (creds["password"] or creds["token"]):
        print("No credentials yet: run `posthouse.py login --url URL --token ph_…`. Until then, "
              f"conversations are only saved locally under {os.path.join(cache_dir(), TRANSCRIPTS_DIR)}.")
    print("Claude Code picks up the change automatically; if it doesn't, open /hooks or restart it.")
    print(f"Log: {os.path.join(cache_dir(), 'hook.log')}")
    return 0


def cmd_uninstall_hooks(a) -> int:
    for scope in (SETTINGS_FILES if a.all else [a.scope]):
        if os.path.exists(_settings_path(scope)):
            print(f"Removed Posthouse hooks from {_edit_settings(scope, install=False)}")
    return 0


def cmd_login(a) -> int:
    creds = {k: getattr(a, k, None) or os.environ.get("POSTHOUSE_" + k.upper()) for k in CRED_KEYS}
    if not creds["url"] or not (creds["password"] or creds["token"]):
        raise SystemExit("posthouse: login needs --url and --token (or --user and --password).")
    me = Client(creds["url"], creds["user"], creds["password"], creds["token"], creds["agent"]).request("GET", "/api/me")
    if not me.get("authenticated"):
        raise SystemExit("posthouse: the server did not accept these credentials.")
    cfg = load_config()
    for k in CRED_KEYS:
        cfg.pop(k, None)
    cfg.update({k: v for k, v in creds.items() if v})
    print(f"Signed in as {me.get('role')}{' (' + me['workspace'] + ')' if me.get('workspace') else ''}; "
          f"saved to {save_config(cfg)} (readable only by you).")
    return 0


def cmd_use(a) -> int:
    d = project_dir()
    if a.clear:
        cfg = load_config()
        (cfg.get("projects") or {}).pop(d, None)
        save_config(cfg)
        print(f"{d} is no longer linked to a workspace.")
    elif a.workspace:
        bind(d, a.workspace, os.environ.get("CLAUDE_CODE_SESSION_ID"))
        print(f"{d} -> workspace {a.workspace}")
    else:
        print(bound_workspace(load_config(), d, os.environ.get("CLAUDE_CODE_SESSION_ID")) or "(no workspace linked)")
    return 0


LOCAL_COMMANDS = {"hook", "install-hooks", "uninstall-hooks", "login", "use", "save-transcript"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="posthouse.py", description="Posthouse client",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
    global_opts = (("--url", "server URL (default: $POSTHOUSE_URL or the saved login)"), ("--user", None),
                   ("--password", None), ("--token", None), ("--agent", "author name recorded with changes"))
    for flag, text in global_opts:
        p.add_argument(flag, help=text)
    p.add_argument("--version", action="version", version=__version__)
    # the same options are also accepted after the command (`login --url … --token …`)
    common = argparse.ArgumentParser(add_help=False)
    for flag, text in global_opts:
        common.add_argument(flag, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_, parents=[common])
        sp.set_defaults(fn=fn)
        return sp

    add("info", cmd_info, "server info and your access level")
    s = add("ls", cmd_ls, "list workspaces"); s.add_argument("--json", action="store_true")
    s = add("create", cmd_create, "create a workspace")
    s.add_argument("name"); s.add_argument("-d", "--description")
    s.add_argument("--workspace-password", dest="ws_password", help="password that unlocks only this workspace")
    s.add_argument("--empty", action="store_true", help="do not add the standard files")
    s = add("start", cmd_start, "print the session bundle (guide, status, journal, files)"); s.add_argument("workspace")
    s = add("tree", cmd_tree, "list files"); s.add_argument("workspace"); s.add_argument("path", nargs="?")
    s.add_argument("-r", "--recursive", action="store_true"); s.add_argument("--json", action="store_true")
    s = add("read", cmd_read, "print a file (version goes to stderr)")
    s.add_argument("workspace"); s.add_argument("path"); s.add_argument("--offset", type=int); s.add_argument("--limit", type=int)
    s.add_argument("--rev", help="read an older revision"); s.add_argument("--json", action="store_true")
    s = add("write", cmd_write, "write a file from FILE or stdin")
    s.add_argument("workspace"); s.add_argument("path"); s.add_argument("file", nargs="?", default="-")
    s.add_argument("--expect", help="expected current version (conflict if it changed)")
    s.add_argument("--new", action="store_true", help="fail if the file already exists"); s.add_argument("-m", "--message")
    s = add("append", cmd_append, "append FILE or stdin to a file")
    s.add_argument("workspace"); s.add_argument("path"); s.add_argument("file", nargs="?", default="-")
    s = add("edit", cmd_edit, "replace exact text in a file")
    s.add_argument("workspace"); s.add_argument("path"); s.add_argument("--old"); s.add_argument("--new")
    s.add_argument("--old-file"); s.add_argument("--new-file"); s.add_argument("--all", action="store_true")
    s.add_argument("--expect")
    s = add("mv", cmd_mv, "move/rename"); s.add_argument("workspace"); s.add_argument("source"); s.add_argument("destination")
    s.add_argument("--overwrite", action="store_true")
    s = add("rm", cmd_rm, "delete a file or folder (recoverable from history)")
    s.add_argument("workspace"); s.add_argument("path"); s.add_argument("-r", "--recursive", action="store_true")
    s = add("mkdir", cmd_mkdir, "create a folder"); s.add_argument("workspace"); s.add_argument("path")
    s = add("search", cmd_search, "full-text search"); s.add_argument("query"); s.add_argument("-w", "--workspace")
    s.add_argument("--limit", type=int, default=30); s.add_argument("--json", action="store_true")
    s = add("log", cmd_log, "record a hand-off note (and optionally replace STATUS.md)")
    s.add_argument("workspace"); s.add_argument("--summary", required=True); s.add_argument("--next")
    s.add_argument("--status-file", help="file with the full new STATUS.md"); s.add_argument("--agent-name")
    s = add("history", cmd_history, "recent changes"); s.add_argument("workspace"); s.add_argument("path", nargs="?")
    s.add_argument("-n", type=int, default=20); s.add_argument("--json", action="store_true")
    s = add("pull", cmd_pull, "download a workspace as files into DIR"); s.add_argument("workspace"); s.add_argument("dir", nargs="?")
    s = add("push", cmd_push, "upload a local file"); s.add_argument("workspace"); s.add_argument("local")
    s.add_argument("remote", nargs="?", help="destination path (default: file name)")

    add("login", cmd_login, "save the server URL and credentials to the config file (checks them first)")
    s = add("use", cmd_use, "link the current project folder to a workspace (used by the hooks)")
    s.add_argument("workspace", nargs="?"); s.add_argument("--clear", action="store_true")
    s = add("save-transcript", cmd_save_transcript, "save this Claude Code conversation to a workspace (or a file)")
    s.add_argument("-w", "--workspace"); s.add_argument("--transcript", help="transcript .jsonl (default: current session)")
    s.add_argument("-o", "--output", help="write Markdown to this file ('-' = stdout) instead of uploading")
    s.add_argument("--max-result", type=int, default=RESULT_CHARS, help="characters kept per tool result")
    for name, fn, text in (("install-hooks", cmd_install_hooks, "save every conversation turn automatically (Claude Code)"),
                           ("uninstall-hooks", cmd_uninstall_hooks, "remove the conversation-saving hooks")):
        s = add(name, fn, text)
        s.add_argument("--scope", choices=sorted(SETTINGS_FILES), default="user",
                       help="user: ~/.claude/settings.json (default); local: .claude/settings.local.json; "
                            "project: .claude/settings.json (shared with the repo)")
        if name == "install-hooks":
            s.add_argument("-w", "--workspace", help="also link the current folder to this workspace")
        else:
            s.add_argument("--all", action="store_true", help="remove from all three settings files")
    s = add("hook", cmd_hook, "run as a Claude Code hook (reads the event from stdin; used by install-hooks)")
    s.add_argument("-w", "--workspace"); s.add_argument("--event", help="event name if stdin has none")
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        if a.cmd in LOCAL_COMMANDS:
            return a.fn(a)
        creds = resolve_creds(a)
        c = Client(creds["url"], creds["user"], creds["password"], creds["token"], creds["agent"])
        a.fn(c, a)
        return 0
    except PosthouseError as e:
        print(f"posthouse: error {e.status}: {e.message}", file=sys.stderr)
        if e.status == 409:
            cur = e.data.get("current_version")
            if cur:
                print(f"posthouse: current version is {cur}; re-read the file and apply your change again", file=sys.stderr)
            return EXIT_CONFLICT
        return 1


if __name__ == "__main__":
    sys.exit(main())
