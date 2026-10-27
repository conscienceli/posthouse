"""FastAPI application assembly."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import router as api_router
from .auth import Auth, AuthError
from .config import Settings
from .db import DB
from .deps import Ctx
from .mcp_server import router as mcp_router
from .oauth import router as oauth_router
from .setup_message import SKILL_PATH, skill_zip
from .storage import StorageError, Workspaces

log = logging.getLogger("posthouse")
STATIC_DIR = Path(__file__).parent / "static"

UI_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
          "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = DB(settings.db_path)
    store = Workspaces(settings, db)
    auth = Auth(settings, db, store)
    ctx = Ctx(settings=settings, db=db, store=store, auth=auth)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        async def housekeeping():
            while True:
                try:
                    db.purge_expired()
                except Exception:  # pragma: no cover
                    log.exception("housekeeping failed")
                await asyncio.sleep(3600)

        async def compact_history():
            # Pack each workspace's git history now and then (only where it has grown).
            await asyncio.sleep(60)
            while True:
                try:
                    await asyncio.to_thread(store.compact_all)
                except Exception:  # pragma: no cover
                    log.exception("history compaction failed")
                await asyncio.sleep(settings.git_gc_hours * 3600)

        tasks = [asyncio.create_task(housekeeping())]
        if store.git and settings.git_gc_hours > 0:
            tasks.append(asyncio.create_task(compact_history()))
        yield
        for task in tasks:
            task.cancel()

    app = FastAPI(title="Posthouse", version=__version__, lifespan=lifespan,
                  docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.state.ctx = ctx

    @app.exception_handler(StorageError)
    async def storage_error(request: Request, exc: StorageError):
        return JSONResponse({"error": exc.code, "message": exc.message, **exc.data}, status_code=exc.status)

    @app.exception_handler(AuthError)
    async def auth_error(request: Request, exc: AuthError):
        return JSONResponse({"error": "unauthorized", "message": exc.message}, status_code=exc.status)

    @app.exception_handler(OSError)
    async def os_error(request: Request, exc: OSError):
        # e.g. a path component that is too long, or a file used as a folder
        client_side = exc.errno in (errno.ENAMETOOLONG, errno.ENOTDIR, errno.EISDIR, errno.EEXIST, errno.EINVAL)
        if not client_side:
            log.exception("file system error")
        return JSONResponse({"error": "invalid_request" if client_side else "server_error",
                             "message": exc.strerror or "file system error"}, status_code=400 if client_side else 500)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"error": "error", "message": str(exc.detail)}
        return JSONResponse(detail, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        msgs = []
        for e in exc.errors():
            loc = ".".join(str(x) for x in e.get("loc", []) if x not in ("body", "query", "path"))
            msgs.append(f"{loc}: {e.get('msg')}" if loc else str(e.get("msg")))
        return JSONResponse({"error": "invalid_request", "message": "; ".join(msgs)}, status_code=422)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        path = request.url.path
        if path == "/" or path.startswith("/static/"):
            resp.headers.setdefault("Content-Security-Policy", UI_CSP)
            resp.headers.setdefault("X-Frame-Options", "DENY")
        elif ((path.startswith("/api/") and path not in ("/api/docs", "/api/openapi.json"))
              or path.startswith("/mcp") or path.startswith("/skill/")):
            # API responses are data only; even if a browser renders one, nothing may run.
            resp.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; sandbox")
        return resp

    app.include_router(oauth_router)
    app.include_router(mcp_router)
    app.include_router(api_router)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True, "version": __version__}

    @app.get(SKILL_PATH, include_in_schema=False)
    def skill_download():
        data = skill_zip()
        if data is None:
            return JSONResponse({"error": "not_found", "message": "The skill is not bundled with this server."}, 404)
        return Response(data, media_type="application/zip", headers={
            "Content-Disposition": 'attachment; filename="posthouse-skill.zip"', "Cache-Control": "no-cache"})

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
