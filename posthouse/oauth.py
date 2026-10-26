"""A small OAuth 2.1 authorization server so web agents (Claude, ChatGPT, Gemini) can connect.

* Discovery: RFC 9728 protected-resource metadata + RFC 8414 authorization-server metadata
* Client registration: Dynamic Client Registration (RFC 7591) and Client ID Metadata Documents
* Authorization code + PKCE (S256 only); the "consent screen" is a password form
* Refresh tokens are rotated (with a short grace period for concurrent refreshes)
"""

from __future__ import annotations

import base64
import html
import ipaddress
import json
import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import unquote, urlencode, urlparse, urlunparse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from . import __version__
from .auth import AuthError
from .deps import base_url, client_key, get_ctx
from .security import RateLimiter, constant_eq, new_token, pkce_s256, token_hash, verify_password

log = logging.getLogger("posthouse.oauth")
router = APIRouter()

CODE_TTL = 300
REQUEST_TTL = 900
REFRESH_GRACE = 120
MAX_CLIENTS = 5000
SCOPES = ["workspace"]
_register_limiter = RateLimiter(60, 3600)
_token_limiter = RateLimiter(300, 600)
PKCE_RE = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")  # RFC 7636 verifier/challenge charset
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}
BAD_SCHEMES = {"javascript", "data", "file", "vbscript", "about", "blob"}


# --------------------------------------------------------------------------- helpers
def _json(data: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def _oauth_error(error: str, description: str, status: int = 400) -> JSONResponse:
    return _json({"error": error, "error_description": description}, status)


def protected_resource_metadata(request: Request) -> dict:
    base = base_url(request)
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "scopes_supported": SCOPES,
        "bearer_methods_supported": ["header"],
        "resource_name": "Posthouse",
        "resource_documentation": "https://github.com/conscienceli/posthouse",
    }


def authorization_server_metadata(request: Request) -> dict:
    base = base_url(request)
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
        "revocation_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
        "scopes_supported": SCOPES + ["offline_access"],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
        "service_documentation": "https://github.com/conscienceli/posthouse",
    }


def _valid_redirect_uri(uri: str) -> bool:
    try:
        u = urlparse(uri)
    except ValueError:
        return False
    if not u.scheme or u.scheme.lower() in BAD_SCHEMES or u.fragment:
        return False
    if u.scheme == "https":
        return bool(u.netloc)
    if u.scheme == "http":
        return (u.hostname or "") in LOOPBACK_HOSTS
    return True  # private-use schemes for native apps, e.g. cursor://


def _strip_port(uri: str) -> str:
    u = urlparse(uri)
    host = u.hostname or ""
    if host == "::1":
        host = "[::1]"
    return urlunparse((u.scheme, host, u.path, u.params, u.query, ""))


def _redirect_matches(requested: str, registered: list[str]) -> bool:
    if requested in registered:
        return True
    try:
        ru = urlparse(requested)
    except ValueError:
        return False
    if ru.scheme == "http" and (ru.hostname or "") in LOOPBACK_HOSTS:
        # RFC 8252 7.3: loopback redirects may use any port. Claude Code also needs this for 'localhost'.
        req = _strip_port(requested)
        return any(_strip_port(r) == req for r in registered
                   if urlparse(r).scheme == "http" and (urlparse(r).hostname or "") in LOOPBACK_HOSTS)
    return False


def _with_params(uri: str, params: dict) -> str:
    u = urlparse(uri)
    q = u.query + ("&" if u.query else "") + urlencode({k: v for k, v in params.items() if v is not None})
    return urlunparse((u.scheme, u.netloc, u.path, u.params, q, u.fragment))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


CIMD_MAX_BYTES = 128 * 1024
CIMD_DEADLINE = 8.0  # seconds for the whole fetch
_cimd_failures: dict[str, float] = {}
_cimd_slots = threading.BoundedSemaphore(4)  # concurrent fetches; extra requests fail fast


def _global_ips(host: str, port: int) -> list[str]:
    """Resolve host; every address must be globally routable (blocks private, CGNAT, loopback...)."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return []
    ips = [info[4][0] for info in infos]
    if not ips or not all(ipaddress.ip_address(ip.split("%")[0]).is_global for ip in ips):
        return []
    return ips


def _read_limited(resp, deadline: float, sock=None) -> bytes:
    """Read the body with a hard overall deadline (read1 returns after a single recv, so a
    server trickling one byte at a time cannot stretch the fetch)."""
    chunks, size = [], 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("metadata fetch timed out")
        if sock is not None:
            sock.settimeout(max(0.05, remaining))
        chunk = resp.read1(16384)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > CIMD_MAX_BYTES:
            raise ValueError("metadata document too large")
        chunks.append(chunk)


def _fetch_cimd(client_id: str) -> dict:
    u = urlparse(client_id)
    if u.scheme != "https" or not u.hostname or u.fragment or u.username or u.password:
        raise ValueError("client_id metadata URL must be https without credentials or fragment")
    port = u.port or 443
    ips = _global_ips(u.hostname, port)
    if not ips:
        raise ValueError("client_id metadata host is not a public address")
    if not _cimd_slots.acquire(blocking=False):
        raise ValueError("too many client metadata fetches in progress")
    try:
        return _fetch_cimd_locked(client_id, u, port, ips)
    finally:
        _cimd_slots.release()


def _fetch_cimd_locked(client_id: str, u, port: int, ips: list[str]) -> dict:
    deadline = time.monotonic() + CIMD_DEADLINE
    headers = {"Accept": "application/json", "User-Agent": f"posthouse/{__version__}"}
    if urllib.request.getproxies().get("https"):
        # An outbound proxy resolves the name itself; go through it.
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(urllib.request.Request(client_id, headers=headers), timeout=4) as r:
            if r.status != 200:
                raise ValueError(f"metadata document returned HTTP {r.status}")
            raw = _read_limited(r, deadline)
    else:
        # Connect to the address we just checked (no second DNS lookup -> no DNS rebinding).
        import http.client
        import ssl
        conn = http.client.HTTPSConnection(u.hostname, port, timeout=4, context=ssl.create_default_context())
        sock = socket.create_connection((ips[0], port), timeout=4)
        conn.sock = conn._context.wrap_socket(sock, server_hostname=u.hostname)  # type: ignore[attr-defined]
        try:
            conn.request("GET", (u.path or "/") + (f"?{u.query}" if u.query else ""), headers=headers)
            r = conn.getresponse()
            if r.status != 200:
                raise ValueError(f"metadata document returned HTTP {r.status}")
            raw = _read_limited(r, deadline, conn.sock)
        finally:
            conn.close()
    doc = json.loads(raw.decode("utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("metadata document must be a JSON object")
    if doc.get("client_id") != client_id:
        raise ValueError("client_id in the metadata document does not match its URL")
    uris = doc.get("redirect_uris")
    if not isinstance(uris, list) or not uris or not all(isinstance(x, str) and _valid_redirect_uri(x) for x in uris):
        raise ValueError("metadata document has no valid redirect_uris")
    return doc


def resolve_client(ctx, client_id: str) -> dict | None:
    if not client_id:
        return None
    row = ctx.db.one("SELECT * FROM oauth_clients WHERE client_id=?", (client_id,))
    if row and not row["is_cimd"]:
        row["redirect_uris"] = json.loads(row["redirect_uris"])
        return row
    if client_id.startswith("https://") and len(client_id) <= 512:
        if row and row["fetched_at"] and time.time() - row["fetched_at"] < 3600:
            row["redirect_uris"] = json.loads(row["redirect_uris"])
            return row
        if not row and time.time() - _cimd_failures.get(client_id, 0) < 300:
            return None  # failed recently; do not let anyone make us fetch it again and again
        try:
            doc = _fetch_cimd(client_id)
        except (ValueError, OSError, urllib.error.URLError) as e:
            log.warning("CIMD fetch failed for %s: %s", client_id, e)
            if len(_cimd_failures) > 1000:
                _cimd_failures.clear()
            _cimd_failures[client_id] = time.time()
            if row:  # fall back to the cached copy
                row["redirect_uris"] = json.loads(row["redirect_uris"])
                return row
            return None
        name = str(doc.get("client_name") or urlparse(client_id).hostname)[:100]
        now = time.time()
        ctx.db.execute(
            "INSERT INTO oauth_clients(client_id, client_secret_hash, client_name, redirect_uris, auth_method, "
            "is_cimd, created_at, fetched_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(client_id) DO UPDATE SET "
            "client_name=excluded.client_name, redirect_uris=excluded.redirect_uris, fetched_at=excluded.fetched_at",
            (client_id, None, name, json.dumps(doc["redirect_uris"]), "none", 1, now, now))
        row = ctx.db.one("SELECT * FROM oauth_clients WHERE client_id=?", (client_id,))
        row["redirect_uris"] = json.loads(row["redirect_uris"])
        return row
    return None


# ------------------------------------------------------------------------- metadata
@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/{rest:path}")
def prm(request: Request, rest: str = ""):
    return _json(protected_resource_metadata(request))


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/oauth-authorization-server/{rest:path}")
def asm(request: Request, rest: str = ""):
    return _json(authorization_server_metadata(request))


# ---------------------------------------------------------------------- registration
@router.post("/oauth/register")
async def register(request: Request):
    ctx = get_ctx(request)
    key = "reg:" + client_key(request)
    if _register_limiter.blocked(key):
        return _oauth_error("too_many_requests", "Too many client registrations; try later.", 429)
    _register_limiter.failure(key)  # counts registrations, not failures
    try:
        body = await request.json()
        assert isinstance(body, dict)
    except Exception:
        return _oauth_error("invalid_client_metadata", "Body must be a JSON object.")
    uris = body.get("redirect_uris")
    if (not isinstance(uris, list) or not 0 < len(uris) <= 10
            or not all(isinstance(u, str) and len(u) <= 2000 and _valid_redirect_uri(u) for u in uris)):
        return _oauth_error("invalid_redirect_uri", "redirect_uris must be a list of 1-10 valid URIs.")
    method = body.get("token_endpoint_auth_method") or "client_secret_basic"
    if method not in ("none", "client_secret_post", "client_secret_basic"):
        method = "none"
    name = str(body.get("client_name") or "MCP client")[:100]
    count = ctx.db.one("SELECT COUNT(*) AS n FROM oauth_clients")["n"]
    if count >= MAX_CLIENTS:
        ctx.db.execute(
            "DELETE FROM oauth_clients WHERE client_id IN (SELECT client_id FROM oauth_clients WHERE is_cimd=0 "
            "AND client_id NOT IN (SELECT client_id FROM oauth_grants) ORDER BY created_at LIMIT 500)")
    client_id = "ph-" + uuid.uuid4().hex
    secret = new_token("", 32) if method != "none" else None
    now = time.time()
    ctx.db.execute(
        "INSERT INTO oauth_clients(client_id, client_secret_hash, client_name, redirect_uris, auth_method, is_cimd, "
        "created_at) VALUES (?,?,?,?,?,0,?)",
        # Client secrets are long random strings, so a fast hash is enough (and cheap to verify).
        (client_id, "sha256$" + token_hash(secret) if secret else None, name, json.dumps(uris), method, now))
    out = {
        "client_id": client_id,
        "client_id_issued_at": int(now),
        "client_name": name,
        "redirect_uris": uris,
        "token_endpoint_auth_method": method,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": " ".join(SCOPES),
    }
    if secret:
        out.update({"client_secret": secret, "client_secret_expires_at": 0})
    return _json(out, 201)


# ---------------------------------------------------------------------- authorization
I18N_DIR = Path(__file__).parent / "static" / "i18n"
_i18n_cache: dict[str, dict] = {}


def _load_lang(code: str) -> dict:
    if code not in _i18n_cache:
        try:
            _i18n_cache[code] = json.loads((I18N_DIR / f"{code}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _i18n_cache[code] = {}
    return _i18n_cache[code]


def _pick_language(accept_language: str) -> str:
    try:
        codes = [x["code"] for x in json.loads((I18N_DIR / "languages.json").read_text(encoding="utf-8"))]
    except (OSError, ValueError, KeyError, TypeError):
        codes = ["en"]
    lower = {c.lower(): c for c in codes}
    prefs = []
    for i, part in enumerate((accept_language or "").split(",")):
        tag, _, q = part.strip().partition(";q=")
        try:
            weight = float(q) if q else 1.0
        except ValueError:
            weight = 0.0
        if tag:
            prefs.append((-weight, i, tag.strip().lower()))
    for _, _, tag in sorted(prefs):
        if tag in lower:
            return lower[tag]
        primary = tag.split("-")[0]
        for low, code in lower.items():
            if low.split("-")[0] == primary:
                return code
    return "en"


def _translator(request: Request):
    strings = dict(_load_lang("en"))
    code = _pick_language(request.headers.get("accept-language", ""))
    if code != "en":
        strings.update(_load_lang(code))

    def t(key: str, **kw) -> str:
        s = strings.get(key, key)
        for k, v in kw.items():
            s = s.replace("{" + k + "}", str(v))
        return s

    return t, code


LOGIN_PAGE = """<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · {app}</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--muted:#5d6676;--line:#dde1e8;--accent:#3b5bdb;--warn:#a15c00;--err:#c92a2a}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1b2029;--fg:#e8ebf1;--muted:#9aa3b2;--line:#2c3340;--accent:#7c95ff;--warn:#f0a63a;--err:#ff6b6b}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;max-width:420px;width:100%;padding:28px}
h1{font-size:19px;margin:0 0 4px}.sub{color:var(--muted);margin:0 0 18px;font-size:14px}
.client{border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:18px;font-size:14px}
.client b{font-size:15px}.host{color:var(--muted);word-break:break-all}
.warn{color:var(--warn);font-size:13px;margin-top:6px}.err{color:var(--err);margin:0 0 12px;font-size:14px}
label{display:block;font-weight:600;font-size:13px;margin:12px 0 4px}.hint{color:var(--muted);font-weight:400}
input{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--fg);font-size:15px}
input:focus{outline:2px solid var(--accent);outline-offset:-1px}
.row{display:flex;gap:10px;margin-top:20px}button{flex:1;padding:10px;border-radius:8px;font-size:15px;cursor:pointer;border:1px solid var(--line);background:transparent;color:var(--fg)}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
.foot{color:var(--muted);font-size:12px;margin-top:16px}
</style></head><body>
<form class="card" method="post" action="authorize" autocomplete="off">
<h1>{app}</h1>
<p class="sub">{title}</p>
<div class="client">{wants}<br>
<span class="host">{redirects}</span>{loopback_warning}</div>
{error}
<input type="hidden" name="request_id" value="{request_id}">
<label for="password">{l_password}</label>
<input id="password" name="password" type="password" required autofocus>
<label for="workspace">{l_workspace}</label>
<input id="workspace" name="workspace" value="{workspace}" placeholder="{l_placeholder}">
<p class="foot">{help}</p>
<div class="row"><button type="submit" name="action" value="deny" formnovalidate>{l_cancel}</button>
<button class="primary" type="submit" name="action" value="approve">{l_allow}</button></div>
</form></body></html>"""

ERROR_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>body{font:15px/1.5 system-ui,sans-serif;max-width:560px;margin:60px auto;padding:0 16px}
@media (prefers-color-scheme:dark){body{background:#12151b;color:#e8ebf1}}</style></head>
<body><h2>{title}</h2><p>{message}</p></body></html>"""


def _fill(template: str, values: dict) -> str:
    # One pass, so a value can never introduce a placeholder that is expanded later.
    return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), template)


def _f(form, key: str) -> str:
    """A form field as a string ('' if missing or not a plain string, e.g. an uploaded file)."""
    v = form.get(key)
    return v if isinstance(v, str) else ""


def _error_page(request: Request, message: str, status: int = 400) -> HTMLResponse:
    t, _ = _translator(request)
    return HTMLResponse(_fill(ERROR_PAGE, {"title": html.escape(t("oauth.errorTitle")),
                                           "message": html.escape(message)}), status_code=status)


def _render_login(request: Request, req: dict, client: dict, error: str = "", workspace: str = "") -> HTMLResponse:
    t, code = _translator(request)
    e = html.escape
    host = urlparse(req["redirect_uri"]).hostname or req["redirect_uri"]
    loopback = (urlparse(req["redirect_uri"]).hostname or "") in LOOPBACK_HOSTS
    page = _fill(LOGIN_PAGE, {
        "lang": e(code),
        "app": e(t("app.name")),
        "title": e(t("oauth.title")),
        "wants": e(t("oauth.wants", client="\x00")).replace(
            "\x00", f"<b>{e(client.get('client_name') or 'An application')}</b>"),
        "redirects": e(t("oauth.redirects", host=host)),
        "loopback_warning": f'<div class="warn">{e(t("oauth.loopback"))}</div>' if loopback else "",
        "error": f'<p class="err">{e(error)}</p>' if error else "",
        "request_id": e(req["id"]),
        "workspace": e(workspace),
        "l_password": e(t("oauth.password")),
        "l_workspace": e(t("oauth.workspace")) + f' <span class="hint">({e(t("common.optional"))})</span>',
        "l_placeholder": e(t("oauth.workspacePlaceholder")),
        "help": e(t("oauth.help")),
        "l_cancel": e(t("oauth.cancel")),
        "l_allow": e(t("oauth.allow")),
    })
    return HTMLResponse(page, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                       "Content-Security-Policy": "frame-ancestors 'none'",
                                       "Content-Language": code})


@router.get("/oauth/authorize")
def authorize_get(request: Request):
    ctx = get_ctx(request)
    q = request.query_params
    client = resolve_client(ctx, q.get("client_id", ""))
    if not client:
        return _error_page(request, "Unknown client_id. The application must register first (or provide a valid "
                           "client ID metadata document).")
    redirect_uri = q.get("redirect_uri")
    if not redirect_uri:
        if len(client["redirect_uris"]) != 1:
            return _error_page(request, "redirect_uri is required.")
        redirect_uri = client["redirect_uris"][0]
    if not _redirect_matches(redirect_uri, client["redirect_uris"]):
        return _error_page(request, "redirect_uri does not match the registered redirect URIs.")
    # Malformed requests get an error page, not a redirect: anyone can register a client, so
    # redirecting before the user has interacted would turn this server into an open redirector.
    if q.get("response_type") != "code":
        return _error_page(request, "Unsupported response_type (only 'code' is supported).")
    challenge = q.get("code_challenge") or ""
    if not PKCE_RE.match(challenge) or q.get("code_challenge_method", "plain") != "S256":
        return _error_page(request, "PKCE with code_challenge_method=S256 is required.")
    req = {"id": new_token("", 24), "client_id": client["client_id"], "redirect_uri": redirect_uri,
           "state": q.get("state"), "code_challenge": challenge, "scope": q.get("scope") or " ".join(SCOPES),
           "resource": q.get("resource"), "expires_at": time.time() + REQUEST_TTL}
    ctx.db.execute(
        "INSERT INTO oauth_requests(id, client_id, redirect_uri, state, code_challenge, scope, resource, expires_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (req["id"], req["client_id"], req["redirect_uri"], req["state"], req["code_challenge"],
         req["scope"], req["resource"], req["expires_at"]))
    return _render_login(request, req, client)


@router.post("/oauth/authorize")
async def authorize_post(request: Request):
    form = await request.form()
    return await run_in_threadpool(_authorize_post, request, form)


def _authorize_post(request: Request, form):
    ctx = get_ctx(request)
    req = ctx.db.one("SELECT * FROM oauth_requests WHERE id=?", (_f(form, "request_id"),))
    if not req or req["expires_at"] < time.time():
        return _error_page(request, "This authorization request has expired. Start the connection again from your app.")
    client = resolve_client(ctx, req["client_id"]) or {"client_name": req["client_id"]}
    iss = base_url(request)
    if _f(form, "action") == "deny":
        ctx.db.execute("DELETE FROM oauth_requests WHERE id=?", (req["id"],))
        return RedirectResponse(_with_params(req["redirect_uri"], {
            "error": "access_denied", "state": req["state"], "iss": iss}), 303)
    workspace = _f(form, "workspace").strip()[:200]
    try:
        p = ctx.auth.check_password(workspace or None, _f(form, "password"), client_key(request),
                                    scope_workspace=workspace or None)
    except AuthError as e:
        return _render_login(request, req, client, error=e.message, workspace=workspace)
    code = new_token("", 32)
    ctx.db.execute(
        "INSERT INTO oauth_codes(code_hash, client_id, redirect_uri, code_challenge, role, workspace_id, scope, "
        "resource, expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (token_hash(code), req["client_id"], req["redirect_uri"], req["code_challenge"], p.role,
         p.workspace_id, req["scope"], req["resource"], time.time() + CODE_TTL))
    ctx.db.execute("DELETE FROM oauth_requests WHERE id=?", (req["id"],))
    ctx.db.log_activity(client.get("client_name") or "oauth", "oauth.authorize", detail=f"role={p.role}")
    return RedirectResponse(_with_params(req["redirect_uri"], {"code": code, "state": req["state"], "iss": iss}), 303)


# ---------------------------------------------------------------------------- tokens
def _client_auth(request: Request, form) -> tuple[str, str]:
    authz = request.headers.get("authorization", "")
    if authz[:6].lower() == "basic ":
        try:
            raw = base64.b64decode(authz[6:].strip()).decode("utf-8")
        except Exception:
            return "", ""
        cid, _, secret = raw.partition(":")
        return unquote(cid), unquote(secret)
    return _f(form, "client_id"), _f(form, "client_secret")


def _authenticate_client(ctx, request: Request, form) -> tuple[dict | None, JSONResponse | None]:
    cid, secret = _client_auth(request, form)
    client = resolve_client(ctx, cid)
    if not client:
        return None, _oauth_error("invalid_client", "Unknown client.", 401)
    stored = client.get("client_secret_hash")
    if stored:
        ok = bool(secret) and (constant_eq("sha256$" + token_hash(secret), stored) if stored.startswith("sha256$")
                               else verify_password(secret, stored))
        if not ok:
            return None, _oauth_error("invalid_client", "Client authentication failed.", 401)
    return client, None


def _issue_tokens(ctx, grant_id: str, scope: str) -> dict:
    access, refresh = new_token("", 32), new_token("", 40)
    now = time.time()
    ctx.db.execute("INSERT INTO oauth_tokens(token_hash, kind, grant_id, expires_at, created_at) VALUES (?,?,?,?,?)",
                   (token_hash(access), "access", grant_id, now + ctx.settings.access_token_ttl, now))
    ctx.db.execute("INSERT INTO oauth_tokens(token_hash, kind, grant_id, expires_at, created_at) VALUES (?,?,?,?,?)",
                   (token_hash(refresh), "refresh", grant_id, now + ctx.settings.refresh_token_ttl, now))
    return {"access_token": access, "token_type": "Bearer", "expires_in": ctx.settings.access_token_ttl,
            "refresh_token": refresh, "scope": scope or " ".join(SCOPES)}


@router.post("/oauth/token")
async def token(request: Request):
    form = await request.form()
    key = "token:" + client_key(request)
    if _token_limiter.blocked(key):
        return _oauth_error("invalid_request", "Too many failed token requests; try again later.", 429)
    resp = await run_in_threadpool(_token, request, form)
    if resp.status_code >= 400:
        _token_limiter.failure(key)
    return resp


def _token(request: Request, form) -> JSONResponse:
    ctx = get_ctx(request)
    client, err = _authenticate_client(ctx, request, form)
    if err:
        return err
    grant_type = _f(form, "grant_type")
    now = time.time()
    if grant_type == "authorization_code":
        row = ctx.db.one("SELECT * FROM oauth_codes WHERE code_hash=?", (token_hash(_f(form, "code")),))
        if row:
            ctx.db.execute("DELETE FROM oauth_codes WHERE code_hash=?", (row["code_hash"],))  # single use
        if not row or row["expires_at"] < now:
            return _oauth_error("invalid_grant", "Authorization code is invalid or expired.")
        if row["client_id"] != client["client_id"]:
            return _oauth_error("invalid_grant", "Code was issued to another client.")
        if _f(form, "redirect_uri") and _f(form, "redirect_uri") != row["redirect_uri"]:
            return _oauth_error("invalid_grant", "redirect_uri mismatch.")
        verifier = _f(form, "code_verifier")
        if not PKCE_RE.match(verifier) or not constant_eq(pkce_s256(verifier), row["code_challenge"]):
            return _oauth_error("invalid_grant", "PKCE verification failed.")
        grant_id = uuid.uuid4().hex
        ctx.db.execute(
            "INSERT INTO oauth_grants(id, client_id, client_name, role, workspace_id, scope, created_at, last_used_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (grant_id, client["client_id"], client.get("client_name") or "", row["role"], row["workspace_id"],
             row["scope"], now, now))
        return _json(_issue_tokens(ctx, grant_id, row["scope"]))
    if grant_type == "refresh_token":
        row = ctx.db.one(
            "SELECT t.*, g.client_id, g.scope FROM oauth_tokens t JOIN oauth_grants g ON g.id=t.grant_id "
            "WHERE t.token_hash=? AND t.kind='refresh'", (token_hash(_f(form, "refresh_token")),))
        if not row or row["expires_at"] < now:
            return _oauth_error("invalid_grant", "Refresh token is invalid or expired.")
        if row["replaced_at"] and now - row["replaced_at"] > REFRESH_GRACE:
            return _oauth_error("invalid_grant", "Refresh token was already used.")
        if row["client_id"] != client["client_id"]:
            return _oauth_error("invalid_grant", "Refresh token was issued to another client.")
        if not row["replaced_at"]:
            ctx.db.execute("UPDATE oauth_tokens SET replaced_at=?, expires_at=? WHERE token_hash=?",
                           (now, now + REFRESH_GRACE, row["token_hash"]))
        ctx.db.execute("UPDATE oauth_grants SET last_used_at=? WHERE id=?", (now, row["grant_id"]))
        return _json(_issue_tokens(ctx, row["grant_id"], row["scope"]))
    return _oauth_error("unsupported_grant_type", "Use authorization_code or refresh_token.")


@router.post("/oauth/revoke")
async def revoke(request: Request):
    ctx = get_ctx(request)
    form = await request.form()
    row = ctx.db.one("SELECT grant_id FROM oauth_tokens WHERE token_hash=?", (token_hash(_f(form, "token")),))
    if row:
        revoke_grant(ctx, row["grant_id"])
    return Response(status_code=200)


def revoke_grant(ctx, grant_id: str) -> bool:
    with ctx.db.transaction() as db:
        found = db.one("SELECT id FROM oauth_grants WHERE id=?", (grant_id,))
        db.execute("DELETE FROM oauth_tokens WHERE grant_id=?", (grant_id,))
        db.execute("DELETE FROM oauth_grants WHERE id=?", (grant_id,))
    return bool(found)
