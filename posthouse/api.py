"""REST API used by the admin UI, the skill (posthouse.py / curl) and any HTTP client."""

from __future__ import annotations

import base64
import binascii
import mimetypes
import os
import time
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import __version__
from .auth import SESSION_COOKIE, AuthError, Principal, clean_actor
from .deps import Ctx, base_url, client_key, get_ctx, is_https
from .mcp_server import _session_markdown
from .oauth import revoke_grant
from .security import constant_eq
from .setup_message import build_message
from .storage import Forbidden, NotFound, StorageError, is_text, now_iso

router = APIRouter(prefix="/api")
SAFE_INLINE_IMAGES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"}


# -------------------------------------------------------------------------- auth deps
def principal(request: Request) -> Principal:
    ctx = get_ctx(request)
    try:
        p = ctx.auth.from_headers(request.headers.get("authorization", ""),
                                  request.cookies.get(SESSION_COOKIE), client_key(request),
                                  request.headers.get("x-posthouse-agent"))
    except AuthError as e:
        raise HTTPException(e.status, {"error": "unauthorized", "message": e.message})
    if p is None:
        raise HTTPException(401, {"error": "unauthorized", "message": "Authentication required."},
                            headers={"WWW-Authenticate": 'Bearer realm="posthouse"'})
    if p.via == "session" and request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get("x-posthouse-csrf") != "1":
            raise HTTPException(403, {"error": "forbidden", "message": "Missing CSRF header."})
    return p


def admin(p: Principal = Depends(principal)) -> Principal:
    if not p.is_admin:
        raise HTTPException(403, {"error": "forbidden", "message": "Admin password required."})
    return p


def _ws(ctx: Ctx, p: Principal, name: str) -> dict:
    ws = ctx.store.get(name)
    if not p.can_access(ws):
        if p.role == "workspace":  # do not reveal which other workspaces exist
            raise NotFound(f"Workspace '{name}' not found.")
        raise Forbidden(f"You do not have access to workspace '{name}'.")
    return ws


# ---------------------------------------------------------------------------- models
class LoginIn(BaseModel):
    password: str


class WorkspaceIn(BaseModel):
    name: str
    description: str = ""
    password: Optional[str] = None
    template: bool = True


class WorkspacePatch(BaseModel):
    description: Optional[str] = None
    password: Optional[str] = None
    clear_password: bool = False
    new_name: Optional[str] = None


class FileWrite(BaseModel):
    path: str
    content: str
    encoding: str = "utf-8"
    expected_version: Optional[str] = None
    message: Optional[str] = None


class FileEdit(BaseModel):
    path: str
    old_text: str
    new_text: str
    replace_all: bool = False
    expected_version: Optional[str] = None


class FileAppend(BaseModel):
    path: str
    content: str


class FileMove(BaseModel):
    source: str
    destination: str
    overwrite: bool = False


class PathIn(BaseModel):
    path: str


class ProgressIn(BaseModel):
    summary: str
    next_steps: Optional[str] = None
    status: Optional[str] = None
    agent: Optional[str] = None


class TokenIn(BaseModel):
    name: str
    role: str = Field("workspace", pattern="^(agent|workspace)$")
    workspace: Optional[str] = None
    expires_days: Optional[int] = Field(None, ge=1, le=3650)


class SetupIn(BaseModel):
    expires_days: Optional[int] = Field(None, ge=1, le=3650)
    lang: Optional[str] = Field(None, max_length=20)
    agent: Optional[str] = Field(None, max_length=80)


class RestoreIn(BaseModel):
    name: Optional[str] = None


# ------------------------------------------------------------------------------ misc
@router.get("/info")
def info(request: Request):
    ctx = get_ctx(request)
    base = base_url(request)
    return {"name": "Posthouse", "version": __version__, "base_url": base,
            "mcp_url": f"{base}/mcp", "agent_password_enabled": bool(ctx.settings.agent_password),
            "git": ctx.store.git, "max_file_mb": ctx.settings.max_file_bytes // (1024 * 1024),
            "history_compaction_hours": ctx.settings.git_gc_hours if ctx.store.git else 0}


@router.post("/login")
def login(body: LoginIn, request: Request):
    ctx = get_ctx(request)
    key = client_key(request)
    if ctx.auth.limiter.blocked(key):
        raise HTTPException(429, {"error": "rate_limited", "message": "Too many failed attempts. Try later."})
    if not constant_eq(body.password, ctx.settings.admin_password):
        ctx.auth.limiter.failure(key)
        raise HTTPException(401, {"error": "unauthorized", "message": "Wrong admin password."})
    token = ctx.auth.create_session()
    secure = ctx.settings.cookie_secure == "true" or (ctx.settings.cookie_secure == "auto" and is_https(request))
    resp = JSONResponse({"ok": True})
    resp.set_cookie(SESSION_COOKIE, token, max_age=ctx.settings.session_ttl, httponly=True,
                    samesite="strict", secure=secure, path="/")
    ctx.db.log_activity("admin", "admin.login", detail=key)
    return resp


@router.post("/logout")
def logout(request: Request):
    ctx = get_ctx(request)
    ctx.auth.destroy_session(request.cookies.get(SESSION_COOKIE, ""))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.get("/me")
def me(request: Request):
    """Who am I? Returns {"authenticated": false} instead of 401 so the UI can probe quietly."""
    ctx = get_ctx(request)
    try:
        p = ctx.auth.from_headers(request.headers.get("authorization", ""),
                                  request.cookies.get(SESSION_COOKIE), client_key(request),
                                  request.headers.get("x-posthouse-agent"))
    except AuthError as e:
        raise HTTPException(e.status, {"error": "unauthorized", "message": e.message})
    if p is None:
        return {"authenticated": False}
    return {"authenticated": True, **p.describe(ctx.store)}


# ------------------------------------------------------------------------ workspaces
@router.get("/workspaces")
def list_workspaces(request: Request, p: Principal = Depends(principal)):
    s = get_ctx(request).store
    return {"workspaces": [s.public(w) for w in p.visible(s.list())]}


@router.post("/workspaces", status_code=201)
def create_workspace(body: WorkspaceIn, request: Request, p: Principal = Depends(principal)):
    if not p.can_create:
        raise Forbidden("Your credentials cannot create workspaces.")
    s = get_ctx(request).store
    ws = s.create(body.name, body.description, body.password or None, body.template, actor=p.actor)
    return s.public(ws)


@router.get("/workspaces/{name}")
def get_workspace(name: str, request: Request, usage: bool = False, p: Principal = Depends(principal)):
    """Details; `usage=true` also measures the change history on disk (slower for big workspaces)."""
    ctx = get_ctx(request)
    ws = _ws(ctx, p, name)
    out = ctx.store.public(ws)
    if usage:
        out["history_bytes"] = ctx.store.history_bytes(ws)
    return out


@router.post("/workspaces/{name}/compact")
def compact_workspace(name: str, request: Request, p: Principal = Depends(admin)):
    """Pack the workspace's change history now (it also happens automatically every day)."""
    ctx = get_ctx(request)
    return ctx.store.compact(_ws(ctx, p, name), force=True, actor=p.actor)


@router.patch("/workspaces/{name}")
def patch_workspace(name: str, body: WorkspacePatch, request: Request, p: Principal = Depends(principal)):
    ctx = get_ctx(request)
    _ws(ctx, p, name)
    if not p.is_admin and (body.password or body.clear_password or body.new_name):
        raise Forbidden("Only the admin can change passwords or rename workspaces.")
    if p.role == "workspace" and body.description is not None:
        raise Forbidden("Only the admin or an agent can change the description.")
    ws = ctx.store.update(name, description=body.description, password=body.password or None,
                          clear_password=body.clear_password, new_name=body.new_name or None, actor=p.actor)
    return ctx.store.public(ws)


@router.delete("/workspaces/{name}")
def delete_workspace(name: str, request: Request, p: Principal = Depends(admin)):
    return get_ctx(request).store.delete(name, actor=p.actor)


@router.get("/workspaces/{name}/tree")
def tree(name: str, request: Request, path: str = "", recursive: bool = False,
         p: Principal = Depends(principal)):
    ctx = get_ctx(request)
    return ctx.store.tree(_ws(ctx, p, name), path, recursive)


@router.get("/workspaces/{name}/file")
def read_file(name: str, path: str, request: Request, offset: Optional[int] = None, limit: Optional[int] = None,
              revision: Optional[str] = None, p: Principal = Depends(principal)):
    ctx = get_ctx(request)
    return ctx.store.read(_ws(ctx, p, name), path, offset, limit, revision)


@router.put("/workspaces/{name}/file")
def write_file(name: str, body: FileWrite, request: Request, p: Principal = Depends(principal)):
    ctx = get_ctx(request)
    ws = _ws(ctx, p, name)
    if body.encoding == "base64":
        try:
            data = base64.b64decode(body.content, validate=True)
        except (binascii.Error, ValueError):
            raise StorageError("content is not valid base64.")
    elif body.encoding in ("utf-8", "utf8", "text"):
        data = body.content.encode("utf-8")
    else:
        raise StorageError("encoding must be 'utf-8' or 'base64'.")
    return ctx.store.write(ws, body.path, data, body.expected_version, actor=p.actor, message=body.message)


@router.post("/workspaces/{name}/edit")
def edit_file(name: str, body: FileEdit, request: Request, p: Principal = Depends(principal)):
    ctx = get_ctx(request)
    return ctx.store.edit(_ws(ctx, p, name), body.path, body.old_text, body.new_text, body.replace_all,
                          body.expected_version, actor=p.actor)


@router.post("/workspaces/{name}/append")
def append_file(name: str, body: FileAppend, request: Request, p: Principal = Depends(principal)):
    ctx = get_ctx(request)
    return ctx.store.append(_ws(ctx, p, name), body.path, body.content, actor=p.actor)


@router.post("/workspaces/{name}/move")
def move_file(name: str, body: FileMove, request: Request, p: Principal = Depends(principal)):
    ctx = get_ctx(request)
    return ctx.store.move(_ws(ctx, p, name), body.source, body.destination, body.overwrite, actor=p.actor)


@router.post("/workspaces/{name}/mkdir")
def mkdir(name: str, body: PathIn, request: Request, p: Principal = Depends(principal)):
    ctx = get_ctx(request)
    return ctx.store.mkdir(_ws(ctx, p, name), body.path, actor=p.actor)


@router.delete("/workspaces/{name}/file")
def delete_file(name: str, path: str, request: Request, recursive: bool = False,
                p: Principal = Depends(principal)):
    ctx = get_ctx(request)
    return ctx.store.delete_path(_ws(ctx, p, name), path, recursive, actor=p.actor)


@router.get("/workspaces/{name}/raw/{path:path}")
def raw_get(name: str, path: str, request: Request, revision: Optional[str] = None,
            download: bool = False, p: Principal = Depends(principal)):
    ctx = get_ctx(request)
    data, ver = ctx.store.read_bytes(_ws(ctx, p, name), path, revision)
    # Files are written by agents and must never become active content on this origin:
    # only raster images are shown inline as themselves, text is shown as text/plain, and
    # everything else (HTML, SVG, XHTML, XML, PDF, ...) is a download.
    guessed = mimetypes.guess_type(path)[0] or ""
    inline = not download
    if guessed in SAFE_INLINE_IMAGES:
        ctype = guessed
    elif is_text(data) and not download:
        ctype = "text/plain; charset=utf-8"
    else:
        ctype, inline = "application/octet-stream", False
    fname = quote(path.rsplit("/", 1)[-1])
    headers = {"X-Posthouse-Version": ver, "ETag": f'"{ver}"', "Cache-Control": "no-store",
               "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{fname}"}
    return Response(data, media_type=ctype, headers=headers)


@router.put("/workspaces/{name}/raw/{path:path}")
async def raw_put(name: str, path: str, request: Request, append: bool = False,
                  p: Principal = Depends(principal)):
    ctx = get_ctx(request)
    ws = _ws(ctx, p, name)
    data = await request.body()
    if append:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise StorageError("append requires UTF-8 text.")
        return await run_in_threadpool(ctx.store.append, ws, path, text, actor=p.actor)
    expected = (request.headers.get("if-match") or "").strip().strip('"') or None
    if request.headers.get("if-none-match", "").strip() == "*":
        expected = "new"
    msg = request.headers.get("x-posthouse-message")
    return await run_in_threadpool(ctx.store.write, ws, path, data, expected, actor=p.actor, message=msg)


@router.post("/workspaces/{name}/upload")
async def upload(name: str, request: Request, dir: str = Form(""), files: list[UploadFile] = File(...),
                 p: Principal = Depends(principal)):
    ctx = get_ctx(request)
    ws = _ws(ctx, p, name)
    out = []
    for f in files:
        data = await f.read()
        fname = (f.filename or "upload.bin").replace("\\", "/").rsplit("/", 1)[-1]
        rel = f"{dir.strip('/')}/{fname}" if dir.strip("/") else fname
        out.append(await run_in_threadpool(ctx.store.write, ws, rel, data, actor=p.actor, message=f"Upload {rel}"))
    return {"uploaded": out}


@router.get("/workspaces/{name}/archive")
def archive(name: str, request: Request, p: Principal = Depends(principal)):
    ctx = get_ctx(request)
    ws = _ws(ctx, p, name)
    data = ctx.store.archive(ws)
    fname = f"{ws['name']}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"})


@router.get("/workspaces/{name}/session")
def session(name: str, request: Request, format: str = "json", p: Principal = Depends(principal)):
    ctx = get_ctx(request)
    bundle = ctx.store.session_bundle(_ws(ctx, p, name))
    if format in ("md", "markdown", "text"):
        return PlainTextResponse(_session_markdown(bundle))
    return bundle


@router.post("/workspaces/{name}/progress")
def progress(name: str, body: ProgressIn, request: Request, p: Principal = Depends(principal)):
    ctx = get_ctx(request)
    return ctx.store.log_progress(_ws(ctx, p, name), body.agent or p.actor, body.summary, body.next_steps,
                                  body.status, actor=p.actor)


@router.get("/workspaces/{name}/history")
def history(name: str, request: Request, path: Optional[str] = None, limit: int = 30,
            p: Principal = Depends(principal)):
    ctx = get_ctx(request)
    return {"commits": ctx.store.history(_ws(ctx, p, name), path, limit)}


@router.get("/search")
def search(q: str, request: Request, workspace: Optional[str] = None, limit: int = 30,
           p: Principal = Depends(principal)):
    ctx = get_ctx(request)
    spaces = [_ws(ctx, p, workspace)] if workspace else p.visible(ctx.store.list())
    return {"results": ctx.store.search(q, spaces, max_results=max(1, min(limit, 200)))}


@router.get("/activity")
def activity(request: Request, workspace: Optional[str] = None, limit: int = 100,
             p: Principal = Depends(principal)):
    ctx = get_ctx(request)
    limit = max(1, min(limit, 500))
    if workspace:
        _ws(ctx, p, workspace)
        rows = ctx.db.all("SELECT * FROM activity WHERE workspace=? ORDER BY id DESC LIMIT ?", (workspace, limit))
    elif p.is_admin:
        rows = ctx.db.all("SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,))
    else:
        names = [w["name"] for w in p.visible(ctx.store.list())]
        if not names:
            return {"activity": []}
        marks = ",".join("?" * len(names))
        rows = ctx.db.all(f"SELECT * FROM activity WHERE workspace IN ({marks}) ORDER BY id DESC LIMIT ?",
                          (*names, limit))
    for r in rows:
        r["time"] = now_iso(r.pop("ts"))
    return {"activity": rows}


# ------------------------------------------------------------------- admin: server settings
@router.get("/settings")
def server_settings(request: Request, p: Principal = Depends(admin)):
    """Effective configuration (never passwords) and disk use, for the admin Settings page."""
    ctx = get_ctx(request)
    s = ctx.settings
    base = base_url(request)
    return {
        "version": __version__, "base_url": base, "mcp_url": f"{base}/mcp", "public_url": s.public_url or None,
        "data_dir": str(s.data_dir), "git": ctx.store.git,
        "history_compaction_hours": s.git_gc_hours if ctx.store.git else 0,
        "last_compaction": ctx.store.last_compaction(),
        "max_file_mb": s.max_file_bytes // (1024 * 1024),
        "agent_password_enabled": bool(s.agent_password),
        "access_token_minutes": s.access_token_ttl // 60, "refresh_token_days": s.refresh_token_ttl // 86400,
        "session_hours": s.session_ttl // 3600,
        "login_max_failures": s.login_max_failures, "login_window_minutes": max(1, s.login_window_seconds // 60),
        "trusted_proxies": os.environ.get("POSTHOUSE_TRUSTED_PROXIES") or "127.0.0.1,::1",
        "usage": ctx.store.usage(),
    }


@router.post("/compact-all")
def compact_all_workspaces(request: Request, p: Principal = Depends(admin)):
    """Compact the change history of every workspace now (it also runs automatically)."""
    ctx = get_ctx(request)
    results = ctx.store.compact_all(force=True, actor=p.actor)
    return {"results": results, "last_compaction": ctx.store.last_compaction()}


# ------------------------------------------------------------------- admin: tokens
@router.get("/tokens")
def list_tokens(request: Request, p: Principal = Depends(admin)):
    ctx = get_ctx(request)
    out = []
    for t in ctx.db.all("SELECT * FROM api_tokens ORDER BY created_at DESC"):
        ws = ctx.store.get_by_id(t["workspace_id"]) if t["workspace_id"] else None
        out.append({"id": t["id"], "name": t["name"], "prefix": t["prefix"], "role": t["role"],
                    "workspace": ws["name"] if ws else None, "created_at": now_iso(t["created_at"]),
                    "last_used_at": now_iso(t["last_used_at"]) if t["last_used_at"] else None,
                    "expires_at": now_iso(t["expires_at"]) if t["expires_at"] else None})
    return {"tokens": out}


@router.post("/tokens", status_code=201)
def create_token(body: TokenIn, request: Request, p: Principal = Depends(admin)):
    ctx = get_ctx(request)
    ws_id = None
    if body.role == "workspace":
        if not body.workspace:
            raise StorageError("workspace is required for a workspace token.")
        ws_id = ctx.store.get(body.workspace)["id"]
    try:
        token, row = ctx.auth.create_api_token(body.name, body.role, ws_id, body.expires_days)
    except AuthError as e:
        raise StorageError(e.message)
    base = base_url(request)
    ctx.db.log_activity(p.actor, "token.create", body.workspace, detail=f"{row['name']} ({body.role})")
    return {"id": row["id"], "name": row["name"], "token": token, "role": body.role, "workspace": body.workspace,
            "mcp_url": f"{base}/mcp", "mcp_url_with_token": f"{base}/mcp/t/{token}"}


@router.delete("/tokens/{token_id}")
def delete_token(token_id: str, request: Request, p: Principal = Depends(admin)):
    ctx = get_ctx(request)
    if not ctx.db.one("SELECT 1 FROM api_tokens WHERE id=?", (token_id,)):
        raise NotFound("Token not found.")
    ctx.db.execute("DELETE FROM api_tokens WHERE id=?", (token_id,))
    ctx.db.log_activity(p.actor, "token.revoke", detail=token_id)
    return {"revoked": True}


def _setup_message(ctx: Ctx, request: Request, p: Principal, body: SetupIn, ws: dict | None) -> dict:
    """Tokens are stored hashed and can't be shown again, so every message gets a new token."""
    stamp = time.strftime("%Y-%m-%d %H:%M", time.gmtime())
    if ws:
        token, row = ctx.auth.create_api_token(f"{ws['name']} · setup message {stamp}", "workspace", ws["id"],
                                               body.expires_days)
    else:
        token, row = ctx.auth.create_api_token(f"setup message {stamp} (all workspaces)", "agent", None,
                                               body.expires_days)
    base = base_url(request)
    agent = clean_actor(body.agent) or "Claude"
    ctx.db.log_activity(p.actor, "token.create", ws["name"] if ws else None,
                        detail=f"{row['name']} ({'workspace' if ws else 'agent'})")
    return {"message": build_message(ws["name"] if ws else None, base, token, agent, body.lang),
            "token_id": row["id"], "token_name": row["name"], "role": row["role"], "base_url": base,
            "expires_at": now_iso(row["expires_at"]) if row["expires_at"] else None}


@router.post("/workspaces/{name}/setup-message", status_code=201)
def setup_message(name: str, body: SetupIn, request: Request, p: Principal = Depends(admin)):
    """A message for a new Claude task, with a token that opens only this workspace."""
    ctx = get_ctx(request)
    return _setup_message(ctx, request, p, body, _ws(ctx, p, name))


@router.post("/setup-message", status_code=201)
def setup_message_all(body: SetupIn, request: Request, p: Principal = Depends(admin)):
    """A message for a new Claude task, with an agent token: all workspaces, and it can create new ones."""
    return _setup_message(get_ctx(request), request, p, body, None)


# -------------------------------------------------------------- admin: connections
@router.get("/connections")
def list_connections(request: Request, p: Principal = Depends(admin)):
    ctx = get_ctx(request)
    out = []
    for g in ctx.db.all("SELECT * FROM oauth_grants ORDER BY COALESCE(last_used_at, created_at) DESC"):
        ws = ctx.store.get_by_id(g["workspace_id"]) if g["workspace_id"] else None
        out.append({"id": g["id"], "client_name": g["client_name"] or g["client_id"], "client_id": g["client_id"],
                    "role": g["role"], "workspace": ws["name"] if ws else None,
                    "created_at": now_iso(g["created_at"]),
                    "last_used_at": now_iso(g["last_used_at"]) if g["last_used_at"] else None})
    return {"connections": out}


@router.delete("/connections/{grant_id}")
def delete_connection(grant_id: str, request: Request, p: Principal = Depends(admin)):
    ctx = get_ctx(request)
    if not revoke_grant(ctx, grant_id):
        raise NotFound("Connection not found.")
    ctx.db.log_activity(p.actor, "oauth.revoke", detail=grant_id)
    return {"revoked": True}


# --------------------------------------------------------------------- admin: trash
@router.get("/trash")
def trash(request: Request, p: Principal = Depends(admin)):
    return {"trash": get_ctx(request).store.trash_list()}


@router.post("/trash/{trash_id}/restore")
def trash_restore(trash_id: str, body: RestoreIn, request: Request, p: Principal = Depends(admin)):
    s = get_ctx(request).store
    return s.public(s.restore(trash_id, body.name, actor=p.actor))


@router.delete("/trash/{trash_id}")
def trash_purge(trash_id: str, request: Request, p: Principal = Depends(admin)):
    get_ctx(request).store.purge(trash_id, actor=p.actor)
    return {"purged": True}
