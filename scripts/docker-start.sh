#!/usr/bin/env bash
# Build and run Posthouse with Docker.
#
#   scripts/docker-start.sh                                    # HTTP on port 8080 (testing / LAN)
#   scripts/docker-start.sh --domain posthouse.example.com     # automatic HTTPS with the bundled Caddy (ports 80/443)
#   scripts/docker-start.sh --behind-proxy https://posthouse.example.com   # your nginx / Cloudflare in front
#   scripts/docker-start.sh --behind-proxy https://posthouse.example.com --tunnel-token <TOKEN>   # Cloudflare Tunnel
#   scripts/docker-start.sh logs | status | stop | restart | update | passwords
set -euo pipefail
source "$(dirname "$0")/lib.sh"
cd "$ROOT_DIR"

usage() {
  sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Options for "up" (the default command). Settings are saved in .env, so you only pass them once.
  --domain DOMAIN         serve https://DOMAIN with Caddy + Let's Encrypt (DNS must point here, ports 80/443 free)
  --behind-proxy URL      an existing reverse proxy (nginx, Caddy, Cloudflare) serves URL and forwards to this
                          server: listen on 127.0.0.1 only, trust the proxy's X-Forwarded-For, use URL publicly
  --tunnel-token TOKEN    also run a Cloudflare Tunnel (cloudflared) with this token; no inbound ports needed.
                          In Cloudflare, point the tunnel's public hostname at http://posthouse:8080
  --no-tunnel             stop and forget the Cloudflare Tunnel
  --direct                plain HTTP on all interfaces again (undo --domain / --behind-proxy; alias: --no-https)
  --port PORT             host port for the app (default 8080)
  --public-url URL        only set the public base URL
  --no-agent-password     do not generate an agent password
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

CMD="up"
case "${1:-}" in
  up|start|stop|down|restart|logs|status|update|passwords|help|-h|--help) CMD="$1"; shift ;;
esac

DOMAIN=""; PORT=""; PUBLIC_URL=""; BEHIND=""; TUNNEL_TOKEN=""; NO_TUNNEL=0; DIRECT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="${2:?--domain needs a value}"; shift 2 ;;
    --behind-proxy) BEHIND="${2:?--behind-proxy needs the public URL, e.g. https://posthouse.example.com}"; shift 2 ;;
    --tunnel-token) TUNNEL_TOKEN="${2:?--tunnel-token needs a value}"; shift 2 ;;
    --no-tunnel) NO_TUNNEL=1; shift ;;
    --direct|--no-https) DIRECT=1; shift ;;
    --port) PORT="${2:?--port needs a value}"; shift 2 ;;
    --public-url) PUBLIC_URL="${2:?--public-url needs a value}"; shift 2 ;;
    --no-agent-password) export POSTHOUSE_NO_AGENT_PASSWORD=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1 (see --help)" ;;
  esac
done
if [ "$CMD" = "help" ] || [ "$CMD" = "-h" ] || [ "$CMD" = "--help" ]; then usage; exit 0; fi

modes=0
[ -n "$DOMAIN" ] && modes=$((modes + 1))
[ -n "$BEHIND" ] && modes=$((modes + 1))
[ "$DIRECT" = "1" ] && modes=$((modes + 1))
[ "$modes" -le 1 ] || die "Use only one of --domain, --behind-proxy and --direct."

docker info >/dev/null 2>&1 || die "Cannot talk to the Docker daemon. Is it running? Do you need sudo / the docker group?"

# Proxies reach the app from the Docker network (or from the host through Docker's port forwarding,
# which shows up as the network gateway). Safe to trust because the app port is then bound to 127.0.0.1.
PROXY_NETS="127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"

profiles() {
  if [ -n "$(get_env POSTHOUSE_DOMAIN)" ]; then echo "--profile https"; fi
  if [ -n "$(get_env POSTHOUSE_TUNNEL_TOKEN)" ]; then echo "--profile tunnel"; fi
}

compose() {
  # shellcheck disable=SC2046
  "${COMPOSE[@]}" $(profiles) "$@"
}

stop_service() {  # stop_service SERVICE PROFILE -> remove an optional container that is no longer wanted
  "${COMPOSE[@]}" --profile "$2" rm -sf "$1" >/dev/null 2>&1 || true
}

base_url() {
  local pub; pub="$(get_env POSTHOUSE_PUBLIC_URL)"
  if [ -n "$pub" ]; then echo "$pub"; return; fi
  local host_ip
  host_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo "http://${host_ip:-127.0.0.1}:$(get_env POSTHOUSE_HOST_PORT || true)"
}

wait_healthy() {
  local port; port="$(get_env POSTHOUSE_HOST_PORT)"; port="${port:-8080}"
  for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:$port/healthz" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  warn "The server did not answer on port $port within 60s. Check: scripts/docker-start.sh logs"
  return 1
}

next_steps() {
  local port; port="$(get_env POSTHOUSE_HOST_PORT)"; port="${port:-8080}"
  if [ -n "$(get_env POSTHOUSE_TUNNEL_TOKEN)" ]; then
    info "Cloudflare Tunnel is running. In Cloudflare, the tunnel's public hostname must point to http://posthouse:8080"
  elif [ -n "$(get_env POSTHOUSE_DOMAIN)" ]; then
    info "Caddy is requesting a certificate for $(get_env POSTHOUSE_DOMAIN); the first HTTPS request may take ~30s."
  elif [ "$(get_env POSTHOUSE_BIND)" = "127.0.0.1" ]; then
    info "Listening on 127.0.0.1:$port only. Point your reverse proxy at http://127.0.0.1:$port"
    echo "     (nginx example: deploy/nginx.conf.example; Cloudflare: docs/deploy.md#cloudflare)"
  fi
}

case "$CMD" in
  passwords)
    [ -f "$ENV_FILE" ] || die "No .env yet. Run scripts/docker-start.sh first."
    echo "Admin password: $(get_env POSTHOUSE_ADMIN_PASSWORD)"
    echo "Agent password: $(get_env POSTHOUSE_AGENT_PASSWORD)" ;;
  logs) compose logs -f --tail=200 ;;
  status) compose ps ;;
  stop|down) "${COMPOSE[@]}" --profile https --profile tunnel down; info "Stopped." ;;
  restart)  # recreate so that edits to .env (passwords, URLs) take effect
    compose up -d --force-recreate; wait_healthy && print_summary "$(base_url)" && next_steps ;;
  update)
    if [ -d .git ]; then info "Pulling latest code"; git pull --ff-only; fi
    compose up -d --build
    wait_healthy && print_summary "$(base_url)" && next_steps ;;
  up|start)
    ensure_env
    # Run the container as the owner of ./data.
    mkdir -p data
    if [ "$(id -u)" = "0" ]; then
      uid="$(get_env POSTHOUSE_UID)"; gid="$(get_env POSTHOUSE_GID)"
      chown -R "${uid:-1000}:${gid:-1000}" data
    else
      set_env POSTHOUSE_UID "$(id -u)"; set_env POSTHOUSE_GID "$(id -g)"
    fi
    if [ -n "$PORT" ]; then set_env POSTHOUSE_HOST_PORT "$PORT"; fi
    if [ -z "$(get_env POSTHOUSE_HOST_PORT)" ]; then set_env POSTHOUSE_HOST_PORT 8080; fi

    if [ "$DIRECT" = "1" ]; then
      set_env POSTHOUSE_DOMAIN ""; set_env POSTHOUSE_TUNNEL_TOKEN ""
      set_env POSTHOUSE_BIND "0.0.0.0"; set_env POSTHOUSE_TRUSTED_PROXIES "127.0.0.1,::1"
      if [[ "$(get_env POSTHOUSE_PUBLIC_URL)" == https://* ]]; then set_env POSTHOUSE_PUBLIC_URL ""; fi
      stop_service caddy https; stop_service cloudflared tunnel
    fi
    if [ -n "$DOMAIN" ]; then
      set_env POSTHOUSE_DOMAIN "$DOMAIN"
      set_env POSTHOUSE_PUBLIC_URL "https://$DOMAIN"
      set_env POSTHOUSE_BIND "127.0.0.1"   # only Caddy is exposed publicly
      set_env POSTHOUSE_TRUSTED_PROXIES "$PROXY_NETS"
    fi
    if [ -n "$BEHIND" ]; then
      BEHIND="${BEHIND%/}"
      case "$BEHIND" in
        https://*) ;;
        http://*) warn "Claude, ChatGPT and Gemini only connect to https:// URLs; $BEHIND will work for local tests only." ;;
        *) die "--behind-proxy needs a full URL such as https://posthouse.example.com" ;;
      esac
      set_env POSTHOUSE_PUBLIC_URL "$BEHIND"
      set_env POSTHOUSE_BIND "127.0.0.1"
      set_env POSTHOUSE_TRUSTED_PROXIES "$PROXY_NETS"
      if [ -n "$(get_env POSTHOUSE_DOMAIN)" ]; then set_env POSTHOUSE_DOMAIN ""; stop_service caddy https; fi
    fi
    if [ -n "$PUBLIC_URL" ]; then set_env POSTHOUSE_PUBLIC_URL "${PUBLIC_URL%/}"; fi
    if [ "$NO_TUNNEL" = "1" ]; then set_env POSTHOUSE_TUNNEL_TOKEN ""; stop_service cloudflared tunnel; fi
    if [ -n "$TUNNEL_TOKEN" ]; then
      [ -n "$(get_env POSTHOUSE_PUBLIC_URL)" ] || die "With --tunnel-token also pass --behind-proxy https://<your tunnel hostname>."
      if [ -n "$(get_env POSTHOUSE_DOMAIN)" ]; then die "--tunnel-token cannot be combined with --domain (use --behind-proxy)."; fi
      set_env POSTHOUSE_TUNNEL_TOKEN "$TUNNEL_TOKEN"
      set_env POSTHOUSE_BIND "127.0.0.1"
      set_env POSTHOUSE_TRUSTED_PROXIES "$PROXY_NETS"
    fi

    info "Building and starting containers"
    compose up -d --build
    wait_healthy || exit 1
    print_summary "$(base_url)"
    next_steps ;;
esac
