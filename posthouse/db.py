"""Tiny SQLite layer. One connection, serialized by a lock (the app runs as one process)."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    password_hash TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    prefix TEXT NOT NULL,
    role TEXT NOT NULL,
    workspace_id TEXT,
    created_at REAL NOT NULL,
    last_used_at REAL,
    expires_at REAL
);

CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY,
    client_secret_hash TEXT,
    client_name TEXT NOT NULL DEFAULT '',
    redirect_uris TEXT NOT NULL,
    auth_method TEXT NOT NULL DEFAULT 'none',
    is_cimd INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    fetched_at REAL
);

CREATE TABLE IF NOT EXISTS oauth_requests (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    state TEXT,
    code_challenge TEXT NOT NULL,
    scope TEXT,
    resource TEXT,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_codes (
    code_hash TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    role TEXT NOT NULL,
    workspace_id TEXT,
    scope TEXT,
    resource TEXT,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_grants (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    client_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL,
    workspace_id TEXT,
    scope TEXT,
    created_at REAL NOT NULL,
    last_used_at REAL
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    grant_id TEXT NOT NULL,
    expires_at REAL NOT NULL,
    replaced_at REAL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oauth_tokens_grant ON oauth_tokens(grant_id);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    workspace TEXT,
    path TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_activity_ws ON activity(workspace, ts);
"""


class DB:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def transaction(self):
        return _Tx(self)

    def log_activity(self, actor: str, action: str, workspace: str | None = None,
                     path: str | None = None, detail: str | None = None) -> None:
        self.execute(
            "INSERT INTO activity(ts, actor, action, workspace, path, detail) VALUES (?,?,?,?,?,?)",
            (time.time(), actor, action, workspace, path, detail),
        )

    def purge_expired(self) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute("DELETE FROM oauth_requests WHERE expires_at < ?", (now,))
            self._conn.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (now,))
            self._conn.execute("DELETE FROM oauth_tokens WHERE expires_at < ?", (now,))
            self._conn.execute("DELETE FROM admin_sessions WHERE expires_at < ?", (now,))
            # Grants with no live refresh token are dead connections.
            self._conn.execute(
                "DELETE FROM oauth_grants WHERE id NOT IN "
                "(SELECT grant_id FROM oauth_tokens WHERE kind='refresh')"
            )
            # Keep the activity log bounded.
            self._conn.execute(
                "DELETE FROM activity WHERE id <= (SELECT MAX(id) - 20000 FROM activity)"
            )


class _Tx:
    def __init__(self, db: DB):
        self.db = db

    def __enter__(self):
        self.db._lock.acquire()
        self.db._conn.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.db._conn.execute("COMMIT")
            else:
                self.db._conn.execute("ROLLBACK")
        finally:
            self.db._lock.release()
        return False
