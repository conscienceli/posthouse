"""Who is calling? Three password levels, API tokens, OAuth access tokens and admin sessions.

Levels
------
admin      POSTHOUSE_ADMIN_PASSWORD  - everything, including the admin UI and deleting workspaces
agent      POSTHOUSE_AGENT_PASSWORD  - list/create workspaces, read & write files in every workspace
workspace  per-workspace pwd   - read & write files in that one workspace
"""

from __future__ import annotations

import base64
import re
import threading
import time
import uuid
from dataclasses import dataclass

from .config import Settings
from .db import DB
from .security import RateLimiter, constant_eq, new_token, token_hash, verify_password
from .storage import NotFound, Workspaces

SESSION_COOKIE = "posthouse_session"
API_TOKEN_PREFIX = "ph_"


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class Principal:
    role: str                      # admin | agent | workspace
    workspace_id: str | None = None
    actor: str = "unknown"         # name recorded in history / activity
    via: str = ""                  # session | basic | api_token | oauth | url_token
    token_id: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def can_create(self) -> bool:
        return self.role in ("admin", "agent")

    def can_access(self, ws: dict) -> bool:
        return self.role in ("admin", "agent") or (self.role == "workspace" and ws["id"] == self.workspace_id)

    def visible(self, workspaces: list[dict]) -> list[dict]:
        return [w for w in workspaces if self.can_access(w)]

    def describe(self, store: Workspaces | None = None) -> dict:
        ws_name = None
        if self.workspace_id and store:
            ws = store.get_by_id(self.workspace_id)
            ws_name = ws["name"] if ws else None
        return {"role": self.role, "workspace": ws_name, "actor": self.actor, "via": self.via}


def clean_actor(name: str | None) -> str | None:
    if not name:
        return None
    name = re.sub(r"[\x00-\x1f<>]", "", str(name)).strip()[:60]
    return name or None


class Auth:
    def __init__(self, settings: Settings, db: DB, store: Workspaces):
        self.settings = settings
        self.db = db
        self.store = store
        self.limiter = RateLimiter(settings.login_max_failures, settings.login_window_seconds)
        # PBKDF2 is deliberately slow; remember recent successful checks (in memory only) so that
        # agents sending Basic auth on every request do not pay ~0.1 s of CPU each time.
        self._verified: dict[str, float] = {}
        self._verified_lock = threading.Lock()

    def _verify_workspace_password(self, ws: dict, password: str) -> bool:
        stored = ws.get("password_hash")
        if not stored or not password:
            return False
        key = token_hash(f"{ws['id']}\x00{stored}\x00{password}")
        now = time.time()
        with self._verified_lock:
            if self._verified.get(key, 0) > now:
                return True
        if not verify_password(password, stored):
            return False
        with self._verified_lock:
            if len(self._verified) > 1000:
                self._verified = {k: v for k, v in self._verified.items() if v > now}
            self._verified[key] = now + 600
        return True

    # ------------------------------------------------------------- passwords
    def check_password(self, username: str | None, password: str, client_key: str,
                       scope_workspace: str | None = None) -> Principal:
        """Validate a password. Raises AuthError on failure.

        `username` may be 'admin', 'agent', a workspace name or empty. The password decides the
        level. If `scope_workspace` is given, the result is narrowed to that workspace.
        """
        if self.limiter.blocked(client_key):
            raise AuthError("Too many failed attempts. Try again later.", 429)
        username = (username or "").strip()
        password = password or ""
        principal: Principal | None = None
        if constant_eq(password, self.settings.admin_password):
            principal = Principal("admin", actor="admin")
        elif self.settings.agent_password and constant_eq(password, self.settings.agent_password):
            principal = Principal("agent", actor="agent")
        else:
            ws_name = scope_workspace or (username if username.lower() not in ("admin", "agent", "") else None)
            if ws_name:
                try:
                    ws = self.store.get(ws_name)
                except NotFound:
                    ws = None
                if ws and self._verify_workspace_password(ws, password):
                    principal = Principal("workspace", workspace_id=ws["id"], actor=ws["name"])
        if principal is None:
            # Failures are never reset by a success: otherwise anyone holding one valid password
            # could interleave logins to keep guessing the others.
            self.limiter.failure(client_key)
            raise AuthError("Invalid credentials.")
        if scope_workspace:
            try:
                ws = self.store.get(scope_workspace)
            except NotFound:
                raise AuthError(f"Workspace '{scope_workspace}' not found.", 400)
            if not principal.can_access(ws):
                raise AuthError("This password does not grant access to that workspace.")
            principal = Principal("workspace", workspace_id=ws["id"], actor=principal.actor)
        return principal

    # ---------------------------------------------------------------- tokens
    def principal_from_token(self, token: str) -> Principal | None:
        if not token:
            return None
        h = token_hash(token)
        now = time.time()
        if token.startswith(API_TOKEN_PREFIX):
            row = self.db.one("SELECT * FROM api_tokens WHERE token_hash=?", (h,))
            if not row or (row["expires_at"] and row["expires_at"] < now):
                return None
            if not row["last_used_at"] or now - row["last_used_at"] > 60:
                self.db.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?", (now, row["id"]))
            return Principal(row["role"], workspace_id=row["workspace_id"], actor=row["name"],
                             via="api_token", token_id=row["id"])
        row = self.db.one(
            "SELECT g.* FROM oauth_tokens t JOIN oauth_grants g ON g.id = t.grant_id "
            "WHERE t.token_hash=? AND t.kind='access' AND t.expires_at > ?", (h, now))
        if not row:
            return None
        if not row["last_used_at"] or now - row["last_used_at"] > 60:
            self.db.execute("UPDATE oauth_grants SET last_used_at=? WHERE id=?", (now, row["id"]))
        return Principal(row["role"], workspace_id=row["workspace_id"],
                         actor=row["client_name"] or "oauth-client", via="oauth", token_id=row["id"])

    def create_api_token(self, name: str, role: str, workspace_id: str | None,
                         expires_days: int | None = None) -> tuple[str, dict]:
        if role not in ("agent", "workspace"):
            raise AuthError("role must be 'agent' or 'workspace'.", 400)
        if role == "workspace" and not workspace_id:
            raise AuthError("A workspace is required for a workspace token.", 400)
        name = clean_actor(name) or "token"
        token = new_token(API_TOKEN_PREFIX, 32)
        row = {
            "id": uuid.uuid4().hex, "name": name, "token_hash": token_hash(token),
            "prefix": token[:10], "role": role, "workspace_id": workspace_id if role == "workspace" else None,
            "created_at": time.time(), "last_used_at": None,
            "expires_at": time.time() + expires_days * 86400 if expires_days else None,
        }
        self.db.execute(
            "INSERT INTO api_tokens(id,name,token_hash,prefix,role,workspace_id,created_at,last_used_at,expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (row["id"], row["name"], row["token_hash"], row["prefix"], row["role"], row["workspace_id"],
             row["created_at"], row["last_used_at"], row["expires_at"]))
        return token, row

    # -------------------------------------------------------------- sessions
    def create_session(self) -> str:
        token = new_token("", 32)
        now = time.time()
        self.db.execute("INSERT INTO admin_sessions(token_hash, created_at, expires_at) VALUES (?,?,?)",
                        (token_hash(token), now, now + self.settings.session_ttl))
        return token

    def session_valid(self, token: str) -> bool:
        if not token:
            return False
        row = self.db.one("SELECT expires_at FROM admin_sessions WHERE token_hash=?", (token_hash(token),))
        return bool(row and row["expires_at"] > time.time())

    def destroy_session(self, token: str) -> None:
        if token:
            self.db.execute("DELETE FROM admin_sessions WHERE token_hash=?", (token_hash(token),))

    # --------------------------------------------------------------- request
    def from_headers(self, authorization: str, cookie_session: str | None, client_key: str,
                     agent_header: str | None = None) -> Principal | None:
        """Resolve a principal. Returns None when no credentials were sent.

        Raises AuthError when credentials were sent but are invalid.
        """
        authorization = (authorization or "").strip()
        p: Principal | None = None
        if authorization[:7].lower() == "bearer ":
            p = self.principal_from_token(authorization[7:].strip())
            if p is None:
                raise AuthError("Invalid or expired token.")
        elif authorization[:6].lower() == "basic ":
            try:
                raw = base64.b64decode(authorization[6:].strip()).decode("utf-8")
            except Exception:
                raise AuthError("Malformed Basic credentials.")
            user, _, pw = raw.partition(":")
            p = self.check_password(user, pw, client_key)
            p.via = "basic"
        elif cookie_session and self.session_valid(cookie_session):
            p = Principal("admin", actor="admin", via="session")
        if p and p.via != "session":
            custom = clean_actor(agent_header)
            if custom:
                p.actor = custom
        return p
