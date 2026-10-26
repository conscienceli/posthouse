"""Entry point: `python -m caw` (reads .env in the current directory if present)."""

from __future__ import annotations

import argparse
import logging
import os
import re

from .config import Settings, load_dotenv

# Only proxies on this machine may set X-Forwarded-For/-Proto unless configured otherwise.
DEFAULT_TRUSTED_PROXIES = "127.0.0.1,::1"
_URL_TOKEN = re.compile(r"(/mcp/t/)[^/?\s\"]+")


class RedactTokens(logging.Filter):
    """Keep secret URL tokens (/mcp/t/<token>) out of the access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args and isinstance(record.args, tuple):
            record.args = tuple(_URL_TOKEN.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.msg, str):
            record.msg = _URL_TOKEN.sub(r"\1***", record.msg)
        return True


def main() -> None:
    parser = argparse.ArgumentParser(prog="caw", description="Cross-Agent Workspace server")
    parser.add_argument("--host", help="bind address (env CAW_HOST, default 0.0.0.0)")
    parser.add_argument("--port", type=int, help="port (env CAW_PORT, default 8080)")
    parser.add_argument("--data-dir", help="data directory (env CAW_DATA_DIR, default ./data)")
    parser.add_argument("--env-file", default=".env", help="optional .env file to load (default ./.env)")
    parser.add_argument("--log-level", default=os.environ.get("CAW_LOG_LEVEL", "info"))
    args = parser.parse_args()

    load_dotenv(args.env_file)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    overrides = {}
    if args.data_dir:
        overrides["data_dir"] = args.data_dir
    settings = Settings.from_env(**overrides)
    host = args.host or settings.host
    port = args.port or settings.port

    import uvicorn

    from .app import create_app

    app = create_app(settings)
    logging.getLogger("caw").info("Cross-Agent Workspace on http://%s:%s  (data: %s)", host, port, settings.data_dir)
    config = uvicorn.Config(app, host=host, port=port, log_level=args.log_level.lower(), proxy_headers=True,
                            forwarded_allow_ips=os.environ.get("CAW_TRUSTED_PROXIES") or DEFAULT_TRUSTED_PROXIES)
    server = uvicorn.Server(config)  # Config() sets up uvicorn's loggers; add the filter afterwards
    logging.getLogger("uvicorn.access").addFilter(RedactTokens())
    server.run()


if __name__ == "__main__":
    main()
