#!/usr/bin/env bash
# Build and run Cross-Agent Workspace with Docker.
#
#   scripts/docker-start.sh                      # HTTP on port 8080
#   scripts/docker-start.sh --domain caw.example.com   # + automatic HTTPS via Caddy (ports 80/443)
#   scripts/docker-start.sh --port 9000
#   scripts/docker-start.sh logs | status | stop | restart | update | passwords
set -euo pipefail
source "$(dirname "$0")/lib.sh"
cd "$ROOT_DIR"

usage() {
  sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Options for "up" (the default command):
  --domain DOMAIN      serve https://DOMAIN with Caddy + Let's Encrypt (DNS must point here)
  --no-https           turn a previous --domain setup back into plain HTTP
  --port PORT          host port for plain HTTP (default 8080)
  --public-url URL     public base URL if you run your own reverse proxy (e.g. https://caw.example.com)
  --no-agent-password  do not generate an agent password
EOF
}

command -v docker >/dev/null 2>&1 || die "Docker is not installed. See https://docs.docker.com/engine/install/"
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  die "Docker Compose is not available (install the docker-compose-plugin package)."
fi
docker info >/dev/null 2>&1 || die "Cannot talk to the Docker daemon. Is it running? Do you need sudo / the docker group?"

CMD="up"
case "${1:-}" in
  up|start|stop|down|restart|logs|status|update|passwords|help|-h|--help) CMD="$1"; shift ;;
esac

DOMAIN=""; PORT=""; PUBLIC_URL=""; NO_HTTPS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="${2:?--domain needs a value}"; shift 2 ;;
    --port) PORT="${2:?--port needs a value}"; shift 2 ;;
    --public-url) PUBLIC_URL="${2:?--public-url needs a value}"; shift 2 ;;
    --no-https) NO_HTTPS=1; shift ;;
    --no-agent-password) export CAW_NO_AGENT_PASSWORD=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1 (see --help)" ;;
  esac
done

profiles() {
  if [ -n "$(get_env CAW_DOMAIN)" ]; then echo "--profile https"; fi
}

compose() {
  # shellcheck disable=SC2046
  "${COMPOSE[@]}" $(profiles) "$@"
}

base_url() {
  local pub; pub="$(get_env CAW_PUBLIC_URL)"
  if [ -n "$pub" ]; then echo "$pub"; return; fi
  local host_ip
  host_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo "http://${host_ip:-127.0.0.1}:$(get_env CAW_HOST_PORT || true)"
}

wait_healthy() {
  local port; port="$(get_env CAW_HOST_PORT)"; port="${port:-8080}"
  for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:$port/healthz" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  warn "The server did not answer on port $port within 60s. Check: scripts/docker-start.sh logs"
  return 1
}

case "$CMD" in
  help|-h|--help) usage ;;
  passwords)
    [ -f "$ENV_FILE" ] || die "No .env yet. Run scripts/docker-start.sh first."
    echo "Admin password: $(get_env CAW_ADMIN_PASSWORD)"
    echo "Agent password: $(get_env CAW_AGENT_PASSWORD)" ;;
  logs) compose logs -f --tail=200 ;;
  status) compose ps ;;
  stop|down) compose down; info "Stopped." ;;
  restart) compose restart; wait_healthy && print_summary "$(base_url)" ;;
  update)
    if [ -d .git ]; then info "Pulling latest code"; git pull --ff-only; fi
    compose up -d --build
    wait_healthy && print_summary "$(base_url)" ;;
  up|start)
    ensure_env
    # Run the container as the owner of ./data.
    mkdir -p data
    if [ "$(id -u)" = "0" ]; then
      uid="$(get_env CAW_UID)"; gid="$(get_env CAW_GID)"
      chown -R "${uid:-1000}:${gid:-1000}" data
    else
      set_env CAW_UID "$(id -u)"; set_env CAW_GID "$(id -g)"
    fi
    [ -n "$PORT" ] && set_env CAW_HOST_PORT "$PORT"
    [ -z "$(get_env CAW_HOST_PORT)" ] && set_env CAW_HOST_PORT 8080
    if [ "$NO_HTTPS" = "1" ]; then
      set_env CAW_DOMAIN ""; set_env CAW_BIND "0.0.0.0"; set_env CAW_TRUSTED_PROXIES "127.0.0.1,::1"
      [[ "$(get_env CAW_PUBLIC_URL)" == https://* ]] && set_env CAW_PUBLIC_URL ""
      "${COMPOSE[@]}" --profile https stop caddy >/dev/null 2>&1 || true
    fi
    if [ -n "$DOMAIN" ]; then
      set_env CAW_DOMAIN "$DOMAIN"
      set_env CAW_PUBLIC_URL "https://$DOMAIN"
      set_env CAW_BIND "127.0.0.1"   # only Caddy is exposed publicly
      # The app port is only reachable through Caddy (same Docker network), so trust its X-Forwarded-For.
      set_env CAW_TRUSTED_PROXIES "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    fi
    [ -n "$PUBLIC_URL" ] && set_env CAW_PUBLIC_URL "${PUBLIC_URL%/}"
    info "Building and starting containers"
    compose up -d --build
    wait_healthy || exit 1
    print_summary "$(base_url)"
    if [ -n "$(get_env CAW_DOMAIN)" ]; then
      info "Caddy is requesting a certificate for $(get_env CAW_DOMAIN); the first HTTPS request may take ~30s."
    fi ;;
esac
