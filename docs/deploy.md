# Deployment

## What the web apps need

Claude, ChatGPT and Gemini connect **from their own cloud**, not from your computer. Your server
must therefore be:

- reachable from the public internet,
- served over **HTTPS on port 443** with a valid certificate (many agent sandboxes can only reach
  port 443),
- configured with `POSTHOUSE_PUBLIC_URL=https://<domain>` when it runs behind a reverse proxy, so OAuth
  metadata advertises the right address.

### Servers in mainland China

- Mainland cloud providers block ports 80/443 for domains without an ICP filing (备案). Use a
  filed domain, or a server in Hong Kong, Japan, Singapore, the US or similar.
- Connections from the platforms' US-based clouds into mainland China can be slow or unreliable.
  An overseas or Hong Kong server is usually the smoother choice.
- Gemini custom apps are currently limited to US users. Claude and ChatGPT connect from their own
  infrastructure regardless of where you are.

## Docker (recommended)

```bash
./scripts/docker-start.sh                          # HTTP on :8080
./scripts/docker-start.sh --domain posthouse.example.com # HTTPS via the bundled Caddy (ports 80+443)
./scripts/docker-start.sh --port 9000              # another HTTP port
./scripts/docker-start.sh --public-url https://posthouse.example.com   # your own proxy in front
./scripts/docker-start.sh logs | status | restart | stop | update | passwords
```

What the script does:

1. Creates `.env` from `.env.example` with random `POSTHOUSE_ADMIN_PASSWORD` / `POSTHOUSE_AGENT_PASSWORD`.
2. Makes `./data` owned by the user the container runs as (`POSTHOUSE_UID`/`POSTHOUSE_GID`).
3. Runs `docker compose up -d --build`. With `--domain` it enables the `https` profile, which runs
   Caddy with Let's Encrypt and binds the app port to `127.0.0.1` only.
4. Waits for `/healthz` and prints the URLs and passwords.

For `--domain`, create a DNS `A`/`AAAA` record for the domain that points at the server, and open
ports 80 and 443. To go back to plain HTTP, run `--no-https`.

Manual equivalent: `cp .env.example .env`, edit it, then run `docker compose up -d --build` (add
`--profile https` when `POSTHOUSE_DOMAIN` is set).

## Bare metal

```bash
./scripts/install.sh                      # .venv + dependencies + .env
./scripts/run.sh                          # foreground
sudo ./scripts/install.sh --systemd       # systemd service "posthouse" (journalctl -u posthouse -f)
sudo ./scripts/install.sh --systemd --user posthouse --port 8080 --public-url https://posthouse.example.com
```

Requirements: Python 3.10+ (`python3-venv` on Debian/Ubuntu), and `git` for history.

### Your own reverse proxy

**Caddy** (automatic HTTPS):

```
posthouse.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

**nginx**: see [`deploy/nginx.conf.example`](../deploy/nginx.conf.example). Forward `Host`,
`X-Forwarded-For` and `X-Forwarded-Proto`, and set `POSTHOUSE_PUBLIC_URL`.

`POSTHOUSE_TRUSTED_PROXIES` (default `127.0.0.1,::1`) lists the proxies whose `X-Forwarded-For` is
believed; the password rate limit is keyed on that client IP. If your proxy reaches the app from
another address (for example a Docker container in front of the published port), add that address
or range, and make sure **only** the proxy can reach the app port (`POSTHOUSE_BIND=127.0.0.1`).

**Cloudflare Tunnel** works without opening ports: `cloudflared tunnel --url http://localhost:8080`,
or a named tunnel with your domain. Set `POSTHOUSE_PUBLIC_URL` to the tunnel's HTTPS address.

## Restricting who can connect (optional)

Authentication is always required. In addition, you can allow only the platforms' egress ranges at
your firewall or proxy:

- Anthropic (Claude): `160.79.104.0/21` (see the IP address page in Claude's docs)
- OpenAI (ChatGPT): `https://openai.com/chatgpt-connectors.json` (the list changes; refresh it automatically)

Remember to also allow your own IP (admin UI) and any CLI agents.

## Backups and upgrades

- Everything lives in `data/`: `workspaces/` (each is a git repository), `trash/`, and
  `posthouse.sqlite3` (workspace metadata, tokens, OAuth grants, activity). Back up the whole directory,
  for example with `restic`, `borg` or a nightly `tar`.
- Each workspace is a normal git repository. You can also `git clone` it over SSH from the server
  for an offline copy.
- Upgrade: `git pull && ./scripts/docker-start.sh update` (Docker), or
  `git pull && ./scripts/install.sh && sudo systemctl restart posthouse`.

## Passwords

- Change the admin or agent password: edit `.env` and restart. Existing OAuth connections and
  tokens keep working; revoke them in the UI if needed.
- Forgot the admin password: read it from `.env` (`scripts/docker-start.sh passwords`) or from
  `data/admin-password.txt` if it was auto-generated.
- Workspace passwords are set or removed in the admin UI (Workspace → Settings).

## Health and logs

- `GET /healthz` returns `{"ok": true}`. The Docker image has a built-in healthcheck.
- Logs: `scripts/docker-start.sh logs` or `journalctl -u posthouse -f`.
- Activity (who did what) is in the admin UI → Activity. File history is in Workspace → History.
