#!/usr/bin/env bash
# Install Cross-Agent Workspace on a Linux server without Docker.
#
#   scripts/install.sh                 # create .venv, install dependencies, create .env
#   sudo scripts/install.sh --systemd  # ... and run it as a systemd service (starts on boot)
#   scripts/install.sh --port 9000 --public-url https://caw.example.com
set -euo pipefail
source "$(dirname "$0")/lib.sh"
cd "$ROOT_DIR"

SYSTEMD=0; PORT=""; PUBLIC_URL=""; SERVICE_USER="${SUDO_USER:-$(id -un)}"
while [ $# -gt 0 ]; do
  case "$1" in
    --systemd) SYSTEMD=1; shift ;;
    --port) PORT="${2:?}"; shift 2 ;;
    --public-url) PUBLIC_URL="${2:?}"; shift 2 ;;
    --user) SERVICE_USER="${2:?}"; shift 2 ;;
    --no-agent-password) export CAW_NO_AGENT_PASSWORD=1; shift ;;
    -h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; echo "  --user USER  account that runs the service (default: the invoking user)"; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || die "python3 not found. Install Python 3.10+ (e.g. apt install python3 python3-venv)."
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "Python 3.10+ is required (found $("$PY" --version 2>&1))."
command -v git >/dev/null 2>&1 || warn "git not found: workspaces will work, but without change history (apt install git)."

if [ ! -x .venv/bin/python ]; then
  info "Creating virtual environment in .venv"
  "$PY" -m venv .venv || die "Could not create a venv. On Debian/Ubuntu: sudo apt install python3-venv"
fi
info "Installing Python dependencies"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

ensure_env
[ -n "$PORT" ] && set_env CAW_PORT "$PORT"
[ -n "$PUBLIC_URL" ] && set_env CAW_PUBLIC_URL "${PUBLIC_URL%/}"
mkdir -p data
if [ "$(id -u)" = "0" ] && [ "$SERVICE_USER" != "root" ]; then
  chown -R "$SERVICE_USER" data .env .venv
fi

if [ "$SYSTEMD" = "1" ]; then
  [ "$(id -u)" = "0" ] || die "--systemd needs root (run with sudo)."
  command -v systemctl >/dev/null 2>&1 || die "systemctl not found."
  UNIT=/etc/systemd/system/caw.service
  sed -e "s|__DIR__|$ROOT_DIR|g" -e "s|__USER__|$SERVICE_USER|g" deploy/caw.service >"$UNIT"
  systemctl daemon-reload
  systemctl enable --now caw.service
  info "Installed and started systemd service 'caw' (logs: journalctl -u caw -f)"
  port="$(get_env CAW_PORT)"; port="${port:-8080}"
  for _ in $(seq 1 30); do curl -fsS "http://127.0.0.1:$port/healthz" >/dev/null 2>&1 && break; sleep 1; done
  base="$(get_env CAW_PUBLIC_URL)"; base="${base:-http://$(hostname -I 2>/dev/null | awk '{print $1}'):$port}"
  print_summary "$base"
else
  info "Installed. Start the server with: scripts/run.sh"
  echo "     (or install it as a service: sudo scripts/install.sh --systemd)"
fi
