#!/usr/bin/env bash
# Shared helpers for the start/install scripts. Sourced, not executed.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"

if [ -t 1 ]; then
  C_BOLD=$'\033[1m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_RESET=$'\033[0m'
else
  C_BOLD=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_RESET=""
fi

info() { printf '%s==>%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '%s[warn]%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
die()  { printf '%s[error]%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; exit 1; }

# Random alphanumeric secret (no characters that need quoting in .env or sed).
rand_secret() {
  local n="${1:-24}" s=""
  if command -v openssl >/dev/null 2>&1; then
    s="$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c "$n")"
  fi
  if [ "${#s}" -lt "$n" ]; then
    s="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$n")"
  fi
  printf '%s' "$s"
}

get_env() {  # get_env KEY -> value from .env (empty if missing)
  [ -f "$ENV_FILE" ] || return 0
  grep -E "^$1=" "$ENV_FILE" | tail -n1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
}

set_env() {  # set_env KEY VALUE -> create or replace the line in .env
  local key="$1" value="$2" esc
  esc="$(printf '%s' "$value" | sed -e 's/[\/&|]/\\&/g')"
  if grep -qE "^$key=" "$ENV_FILE"; then
    sed -i.bak "s|^$key=.*|$key=$esc|" "$ENV_FILE" && rm -f "$ENV_FILE.bak"
  else
    printf '%s=%s\n' "$key" "$value" >>"$ENV_FILE"
  fi
}

# Create .env from .env.example on first run and fill in passwords that are still empty.
ensure_env() {
  if [ ! -f "$ENV_FILE" ]; then
    cp "$ROOT_DIR/.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE" 2>/dev/null || true
    info "Created .env from .env.example"
  fi
  if [ -z "$(get_env POSTHOUSE_ADMIN_PASSWORD)" ]; then
    set_env POSTHOUSE_ADMIN_PASSWORD "$(rand_secret 24)"
    info "Generated POSTHOUSE_ADMIN_PASSWORD"
  fi
  if ! grep -qE '^POSTHOUSE_AGENT_PASSWORD=' "$ENV_FILE" || [ -z "$(get_env POSTHOUSE_AGENT_PASSWORD)" ]; then
    if [ "${POSTHOUSE_NO_AGENT_PASSWORD:-0}" != "1" ]; then
      set_env POSTHOUSE_AGENT_PASSWORD "$(rand_secret 24)"
      info "Generated POSTHOUSE_AGENT_PASSWORD"
    fi
  fi
}

print_summary() {  # print_summary BASE_URL
  local base="$1"
  echo
  printf '%s%s%s\n' "$C_BOLD" "Posthouse is running" "$C_RESET"
  echo "  Admin UI        : $base/"
  echo "  MCP endpoint    : $base/mcp"
  echo "  REST API docs   : $base/api/docs"
  echo "  Admin password  : $(get_env POSTHOUSE_ADMIN_PASSWORD)"
  if [ -n "$(get_env POSTHOUSE_AGENT_PASSWORD)" ]; then
    echo "  Agent password  : $(get_env POSTHOUSE_AGENT_PASSWORD)"
  fi
  echo "  Data directory  : $ROOT_DIR/data   (passwords are stored in $ENV_FILE)"
  echo
  case "$base" in
    https://*) ;;
    *) warn "Web agents (Claude, ChatGPT, Gemini) need a public HTTPS URL. Use --domain or a reverse proxy; see docs/deploy.md." ;;
  esac
}
