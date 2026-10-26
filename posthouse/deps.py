"""Shared request helpers."""

from __future__ import annotations

from dataclasses import dataclass

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
