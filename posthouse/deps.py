"""Shared request helpers."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote

from fastapi import Request

from .auth import Auth
from .config import Settings
from .db import DB
from .storage import Workspaces


@dataclass
class Ctx:
    settings: Settings
    db: DB
    store: Workspaces
    auth: Auth


def get_ctx(request: Request) -> Ctx:
    return request.app.state.ctx


def base_url(request: Request) -> str:
    ctx = get_ctx(request)
    if ctx.settings.public_url:
        return ctx.settings.public_url
    return str(request.base_url).rstrip("/")


def client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def is_https(request: Request) -> bool:
    return base_url(request).startswith("https://")


def header_text(value: str | None) -> str | None:
    """Text from a header such as X-Posthouse-Message or X-Posthouse-Agent.

    Headers are Latin-1, so non-ASCII text arrives either RFC 8187-encoded (UTF-8''%E4%B8%AD…,
    what posthouse.py sends) or as raw UTF-8 bytes (what curl sends), which the server sees as
    Latin-1 characters. Both become proper text; plain ASCII (even "50% done") is unchanged.
    """
    if not value:
        return value
    if value[:7].lower() == "utf-8''":
        return unquote(value[7:], encoding="utf-8", errors="replace")
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
