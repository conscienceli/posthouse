#!/usr/bin/env bash
# Run Posthouse in the foreground (reads .env). Extra args go to `python -m posthouse`,
# e.g. scripts/run.sh --port 9000
set -euo pipefail
source "$(dirname "$0")/lib.sh"
cd "$ROOT_DIR"

if [ ! -f "$ENV_FILE" ]; then
  ensure_env
fi
if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
else
  PY="${PYTHON:-python3}"
  "$PY" -c 'import fastapi, uvicorn' 2>/dev/null || die "Dependencies missing. Run scripts/install.sh first."
fi
port="$(get_env POSTHOUSE_PORT)"
info "Admin password: $(get_env POSTHOUSE_ADMIN_PASSWORD)   (UI: http://127.0.0.1:${port:-8080}/)"
exec "$PY" -m posthouse "$@"
