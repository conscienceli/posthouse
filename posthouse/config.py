"""Runtime configuration, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("posthouse")


def load_dotenv(path: str | os.PathLike = ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines, '#' comments, optional quotes.

    Values already present in the process environment win, so `docker run -e`
    and systemd `Environment=` always override the file.
    """
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    v = os.environ.get(name)
    try:
        return int(v) if v not in (None, "") else default
    except ValueError:
        return default


@dataclass
class Settings:
    data_dir: Path
    admin_password: str
    agent_password: str = ""
    public_url: str = ""
    host: str = "0.0.0.0"
    port: int = 8080
    max_file_bytes: int = 10 * 1024 * 1024
    git_enabled: bool = True
    git_gc_hours: int = 24
    access_token_ttl: int = 3600
    refresh_token_ttl: int = 180 * 24 * 3600
    session_ttl: int = 12 * 3600
    cookie_secure: str = "auto"  # auto | true | false
    login_max_failures: int = 10
    login_window_seconds: int = 600
    extra: dict = field(default_factory=dict)

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def trash_dir(self) -> Path:
        return self.data_dir / "trash"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "posthouse.sqlite3"

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        data_dir = Path(overrides.pop("data_dir", None) or os.environ.get("POSTHOUSE_DATA_DIR") or "./data").resolve()
        data_dir.mkdir(parents=True, exist_ok=True)

        admin_password = overrides.pop("admin_password", None) or os.environ.get("POSTHOUSE_ADMIN_PASSWORD", "")
        if not admin_password:
            admin_password = _load_or_create_admin_password(data_dir)

        s = cls(
            data_dir=data_dir,
            admin_password=admin_password,
            agent_password=os.environ.get("POSTHOUSE_AGENT_PASSWORD", ""),
            public_url=os.environ.get("POSTHOUSE_PUBLIC_URL", "").rstrip("/"),
            host=os.environ.get("POSTHOUSE_HOST", "0.0.0.0"),
            port=_int("POSTHOUSE_PORT", 8080),
            max_file_bytes=_int("POSTHOUSE_MAX_FILE_MB", 10) * 1024 * 1024,
            git_enabled=_bool("POSTHOUSE_GIT", True),
            git_gc_hours=_int("POSTHOUSE_GIT_GC_HOURS", 24),
            access_token_ttl=_int("POSTHOUSE_ACCESS_TOKEN_TTL", 3600),
            refresh_token_ttl=_int("POSTHOUSE_REFRESH_TOKEN_DAYS", 180) * 24 * 3600,
            session_ttl=_int("POSTHOUSE_SESSION_HOURS", 12) * 3600,
            cookie_secure=os.environ.get("POSTHOUSE_COOKIE_SECURE", "auto").lower(),
            login_max_failures=_int("POSTHOUSE_LOGIN_MAX_FAILURES", 10),
            login_window_seconds=_int("POSTHOUSE_LOGIN_WINDOW_SECONDS", 600),
        )
        for k, v in overrides.items():
            setattr(s, k, v)
        if s.agent_password and s.agent_password == s.admin_password:
            log.warning("POSTHOUSE_AGENT_PASSWORD equals POSTHOUSE_ADMIN_PASSWORD; agents will get admin rights.")
        return s


def _load_or_create_admin_password(data_dir: Path) -> str:
    f = data_dir / "admin-password.txt"
    if f.is_file():
        pw = f.read_text(encoding="utf-8").strip()
        if pw:
            return pw
    pw = secrets.token_urlsafe(18)
    f.write_text(pw + "\n", encoding="utf-8")
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass
    log.warning("POSTHOUSE_ADMIN_PASSWORD is not set. Generated one and saved it to %s", f)
    return pw
