#!/usr/bin/env python3
"""caw.py - command-line client for a Cross-Agent Workspace server (Python 3.8+, stdlib only).

Credentials come from flags, then environment variables:
  CAW_URL       server base URL, e.g. https://caw.example.com
  CAW_USER      'agent', 'admin' or a workspace name (for Basic auth with a password)
  CAW_PASSWORD  the matching password
  CAW_TOKEN     an access token (caw_...) instead of user/password
  CAW_AGENT     your name, recorded as the author of changes (e.g. "Claude Cowork")

Examples:
  caw.py ls
  caw.py start my-project
  caw.py read my-project STATUS.md
  echo "# Status ..." | caw.py write my-project STATUS.md --expect 1a2b3c4d5e6f7a8b
  caw.py edit my-project STATUS.md --old "- [ ] draft" --new "- [x] draft"
  caw.py log my-project --summary "Drafted section 2" --next "Review section 3"
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile

__version__ = "0.1.0"
EXIT_CONFLICT = 3


class CawError(Exception):
    def __init__(self, status: int, message: str, data: dict | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.data = data or {}


class Client:
    def __init__(self, url: str, user: str | None, password: str | None, token: str | None, agent: str | None):
        if not url:
            raise SystemExit("caw: no server URL. Set CAW_URL or pass --url.")
        self.base = url.rstrip("/")
        self.headers = {"User-Agent": f"caw.py/{__version__}"}
        if token:
            self.headers["Authorization"] = "Bearer " + token
        elif password:
            raw = f"{user or 'agent'}:{password}".encode("utf-8")
            self.headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        if agent:
            self.headers["X-CAW-Agent"] = agent

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
            with urllib.request.urlopen(req, timeout=60) as r:
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
            raise CawError(e.code, info.get("message") or str(info), info) from None
        except urllib.error.URLError as e:
            raise CawError(0, f"cannot reach {self.base}: {e.reason}") from None


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
    print(f"version: {headers.get('X-CAW-Version') or headers.get('x-caw-version')}", file=sys.stderr)
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
        h["X-CAW-Message"] = a.message
    out(c.request("PUT", file_url(a.workspace, a.path), raw=data, headers=h))


def cmd_append(c: Client, a):
    out(c.request("PUT", file_url(a.workspace, a.path), raw=read_input(a.file), query={"append": True}))


def _text_arg(text: str | None, path: str | None, name: str) -> str:
    if path:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    if text is None:
        raise SystemExit(f"caw: --{name} or --{name}-file is required")
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
                raise SystemExit(f"caw: refusing unsafe path in archive: {m}")
        z.extractall(dest)
    print(f"extracted to {os.path.join(dest, a.workspace)}")


def cmd_push(c: Client, a):
    remote = a.remote or os.path.basename(a.local)
    with open(a.local, "rb") as f:
        data = f.read()
    out(c.request("PUT", file_url(a.workspace, remote), raw=data))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="caw.py", description="Cross-Agent Workspace client",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
    p.add_argument("--url", default=os.environ.get("CAW_URL"))
    p.add_argument("--user", default=os.environ.get("CAW_USER"))
    p.add_argument("--password", default=os.environ.get("CAW_PASSWORD"))
    p.add_argument("--token", default=os.environ.get("CAW_TOKEN"))
    p.add_argument("--agent", default=os.environ.get("CAW_AGENT"), help="author name recorded with changes")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
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
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    c = Client(a.url, a.user, a.password, a.token, a.agent)
    try:
        a.fn(c, a)
        return 0
    except CawError as e:
        print(f"caw: error {e.status}: {e.message}", file=sys.stderr)
        if e.status == 409:
            cur = e.data.get("current_version")
            if cur:
                print(f"caw: current version is {cur}; re-read the file and apply your change again", file=sys.stderr)
            return EXIT_CONFLICT
        return 1


if __name__ == "__main__":
    sys.exit(main())
