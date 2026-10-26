# Deployment

## What the web apps need

Claude, ChatGPT and Gemini connect **from their own cloud**, not from your computer. Your server
must therefore be:

- reachable from the public internet on a **public IPv4 address** (Claude's connectors are
  IPv4-only, so the hostname needs an `A` record),
- served over **HTTPS on port 443** with a certificate browsers trust (many agent sandboxes can only
  reach port 443),
- on its **own (sub)domain**, e.g. `posthouse.example.com`. OAuth discovery lives at `/.well-known/`
  on the domain root, so Posthouse cannot be served from a sub-path such as `example.com/posthouse/`,
- configured with `POSTHOUSE_PUBLIC_URL=https://<domain>` when a reverse proxy is in front, so OAuth
  metadata advertises the public address. The `--domain` and `--behind-proxy` options do this.

## Choose a setup

| Your situation | Command | Section |
|---|---|---|
| A server with ports 80/443 free and a domain pointing at it | `scripts/docker-start.sh --domain posthouse.example.com` | [Built-in HTTPS](#built-in-https-caddy) |
| nginx (or another proxy) already serves your sites | `scripts/docker-start.sh --behind-proxy https://posthouse.example.com` | [Existing nginx](#behind-an-existing-nginx) |
| Your domain is on Cloudflare | Tunnel: `scripts/docker-start.sh --behind-proxy https://posthouse.example.com --tunnel-token <TOKEN>` | [Cloudflare](#cloudflare) |
| Testing on a LAN | `scripts/docker-start.sh` (HTTP on port 8080) | — |

The options are saved in `.env`, so later runs of `scripts/docker-start.sh` keep them. `--direct`
switches back to plain HTTP on all interfaces. Without Docker, `scripts/install.sh` accepts
`--behind-proxy URL` as well ([Bare metal](#bare-metal)).

### Servers in mainland China

- Mainland cloud providers block ports 80/443 for domains without an ICP filing (备案). Use a
  filed domain, or a server in Hong Kong, Japan, Singapore, the US or similar.
- Connections from the platforms' US-based clouds into mainland China can be slow or unreliable.
  An overseas or Hong Kong server is usually the smoother choice.
- Gemini custom apps are currently limited to US users. Claude and ChatGPT connect from their own
  infrastructure regardless of where you are.

## Docker

```bash
./scripts/docker-start.sh [options]        # create .env, build, start, print URLs and passwords
./scripts/docker-start.sh logs | status | restart | stop | update | passwords
./scripts/docker-start.sh --help
```

What the script does:

1. Creates `.env` from `.env.example` with random `POSTHOUSE_ADMIN_PASSWORD` / `POSTHOUSE_AGENT_PASSWORD`.
2. Makes `./data` owned by the user the container runs as (`POSTHOUSE_UID`/`POSTHOUSE_GID`).
3. Applies the options to `.env` and runs `docker compose up -d --build`, with the `https` profile
   (Caddy) or the `tunnel` profile (cloudflared) when they are configured.
4. Waits for `/healthz` and prints the URLs and passwords.

Manual equivalent: `cp .env.example .env`, edit it, then `docker compose up -d --build` (add
`--profile https` when `POSTHOUSE_DOMAIN` is set, `--profile tunnel` when `POSTHOUSE_TUNNEL_TOKEN` is set).

### Built-in HTTPS (Caddy)

```bash
./scripts/docker-start.sh --domain posthouse.example.com
```

Create a DNS `A` record for the domain that points at the server, and make sure ports 80 and 443
are free and open. Caddy obtains and renews a Let's Encrypt certificate. The app port is bound to
`127.0.0.1`, so only Caddy is exposed.

## Behind an existing nginx

1. **DNS and certificate.** Point `posthouse.example.com` at the server and get a certificate, e.g.
   `sudo certbot certonly --nginx -d posthouse.example.com`.
2. **Start Posthouse behind the proxy:**

   ```bash
   ./scripts/docker-start.sh --behind-proxy https://posthouse.example.com
   ```

   This sets `POSTHOUSE_PUBLIC_URL`, binds the app to `127.0.0.1:8080` only (`--port` to change it)
   and trusts `X-Forwarded-For` from local and Docker-internal addresses. That trust is needed
   because nginx on the host reaches the container through Docker's port forwarding, so the app sees
   the Docker gateway (e.g. `172.18.0.1`), not `127.0.0.1`. It is safe because nothing else can
   reach the loopback-only port.
3. **nginx site.** Copy [`deploy/nginx.conf.example`](../deploy/nginx.conf.example) to
   `/etc/nginx/sites-available/posthouse` (and symlink it into `sites-enabled`) or to
   `/etc/nginx/conf.d/posthouse.conf`, replace the domain, then
   `sudo nginx -t && sudo systemctl reload nginx`.
4. **Check** from outside the server:

   ```bash
   curl -i -X POST https://posthouse.example.com/mcp
   #   -> 401 with: WWW-Authenticate: Bearer resource_metadata="https://posthouse.example.com/..."
   curl https://posthouse.example.com/.well-known/oauth-authorization-server
   #   -> "issuer": "https://posthouse.example.com"
   ```

Common problems:

- **A "deny dot-files" rule** such as `location ~ /\. { deny all; }` also blocks `/.well-known/` and
  breaks the OAuth sign-in. Use `location ~ /\.(?!well-known) { deny all; }`.
- **`413 Request Entity Too Large`**: nginx accepts only 1 MB bodies by default; set
  `client_max_body_size 20m;` (the example does).
- **nginx itself runs in Docker**: put both containers on one Docker network and use
  `proxy_pass http://posthouse:8080;`.
- **Caddy instead of nginx**: `posthouse.example.com { reverse_proxy 127.0.0.1:8080 }`.

## Cloudflare

If your domain is on Cloudflare, there are three ways to connect it to Posthouse. Cloudflare
terminates HTTPS for visitors in all of them; they differ in how Cloudflare reaches your server.

| | Cloudflare → your server | Open ports | Certificate on your server | Verdict |
|---|---|---|---|---|
| **A. Cloudflare Tunnel** | Encrypted tunnel opened *from* your server | none | none | **Recommended** |
| **B. Proxy + "Full (strict)"** | HTTPS to your nginx | 443 | free Cloudflare Origin Certificate | Good |
| **C. Proxy + "Flexible"** | **Plain HTTP** to your nginx | 80 | none | Avoid: passwords, tokens and files travel unencrypted between Cloudflare and you |

In all three cases Posthouse runs with `--behind-proxy https://posthouse.example.com`.

### A. Cloudflare Tunnel (recommended)

No inbound ports, no certificate, encrypted all the way, and it also works when the server has no
public IP.

1. In the Cloudflare dashboard, open **Networking → Tunnels** (formerly Zero Trust → Networks →
   Tunnels), select **Create tunnel**, choose **cloudflared**, and copy the **tunnel token** shown in
   the install command.
2. On the server:

   ```bash
   ./scripts/docker-start.sh --behind-proxy https://posthouse.example.com --tunnel-token <TOKEN>
   ```

   This runs `cloudflared` as a second container (`posthouse-tunnel`) next to the app. The token is
   stored in `.env`.
3. In the tunnel, add a **public hostname / published application**: hostname
   `posthouse.example.com`, service **`http://posthouse:8080`** (the app's name on the Docker network).
   Cloudflare creates the DNS record.

Without Docker, install cloudflared as a service (`sudo cloudflared service install <TOKEN>`), run
`sudo scripts/install.sh --systemd --behind-proxy https://posthouse.example.com`, and use
`http://localhost:8080` as the service URL.

Real visitor IPs work automatically: Cloudflare adds the visitor to `X-Forwarded-For`, and Posthouse
trusts the cloudflared container. To remove the tunnel: `--no-tunnel`.

### B. Cloudflare proxy with "Full (strict)"

Use this if you already run nginx with the orange cloud enabled.

1. **Origin certificate**: Cloudflare dashboard → SSL/TLS → Origin Server → Create Certificate.
   Save the certificate and private key on the server (e.g. under `/etc/ssl/cloudflare/`). It is
   free and valid for years, but only Cloudflare trusts it, so keep the DNS record proxied.
2. **SSL/TLS encryption mode**: Full (strict).
3. **Posthouse**: `./scripts/docker-start.sh --behind-proxy https://posthouse.example.com`
4. **Real visitor IPs**: `scripts/cloudflare-realip.sh | sudo tee /etc/nginx/conf.d/cloudflare-realip.conf`.
   It writes `set_real_ip_from` lines for Cloudflare's ranges plus `real_ip_header CF-Connecting-IP;`.
   Re-run it occasionally; Cloudflare adds ranges now and then. Without it, every visitor looks like
   a Cloudflare address, and the password rate limit would lump them together.
5. **nginx site**: Option A in [`deploy/nginx-cloudflare.conf.example`](../deploy/nginx-cloudflare.conf.example).
6. Optional: allow ports 80/443 only from [Cloudflare's IP ranges](https://www.cloudflare.com/ips/)
   in your firewall, so nobody can bypass Cloudflare.

### C. Cloudflare proxy with "Flexible"

Your server only speaks HTTP and Cloudflare adds HTTPS for visitors. It works: run
`--behind-proxy https://posthouse.example.com` and use option B in
[`deploy/nginx-cloudflare.conf.example`](../deploy/nginx-cloudflare.conf.example), plus the real-IP
snippet from B.4. But the leg between Cloudflare and your server is unencrypted, and it carries
passwords, OAuth tokens and file contents. Prefer A or B. Do not redirect HTTP to HTTPS on the
server in this mode; Cloudflare already does that for visitors, and an origin redirect causes an
endless redirect loop.

### Cloudflare settings that break agents

| Setting | Problem | What to do |
|---|---|---|
| **Bot Fight Mode** (Security → Bots) | Challenges server-to-server requests from Claude/ChatGPT/Gemini with a 403 page that never reaches Posthouse. On the Free plan it cannot be skipped per path | Turn it off for the zone. Pro and higher: use Super Bot Fight Mode with a WAF *Skip* rule for `posthouse.example.com` |
| **"I'm Under Attack" mode / managed challenges / WAF rules** | Same: agents cannot solve JavaScript challenges | Don't enable them for this hostname, or add a skip rule |
| **Cloudflare Access** in front of the hostname | Asks for a Cloudflare login that agents can't do | Don't protect this hostname with Access; Posthouse has its own authentication |
| **Rocket Loader** (Speed → Optimization) | Rewrites the admin UI's scripts; the page's Content-Security-Policy blocks them and the UI stays blank | Turn it off, or add a Configuration Rule that disables it for this hostname |
| **Always Use HTTPS** (SSL/TLS → Edge Certificates) | — | Keep it on |

If an app reports "Couldn't reach the MCP server" but `curl` works from your laptop, check
**Security → Events** in Cloudflare for blocked requests.

## Bare metal

```bash
./scripts/install.sh                      # .venv + dependencies + .env
./scripts/run.sh                          # foreground
sudo ./scripts/install.sh --systemd       # systemd service "posthouse" (journalctl -u posthouse -f)
sudo ./scripts/install.sh --systemd --behind-proxy https://posthouse.example.com   # nginx/Cloudflare in front
```

Requirements: Python 3.10+ (`python3-venv` on Debian/Ubuntu), and `git` for history.
`--behind-proxy` sets `POSTHOUSE_PUBLIC_URL`, makes the app listen on `127.0.0.1` only and trusts
`X-Forwarded-For` from local proxies. Use `proxy_pass http://127.0.0.1:8080;` in nginx.

### Trusted proxies

`POSTHOUSE_TRUSTED_PROXIES` lists the addresses whose `X-Forwarded-For` / `X-Forwarded-Proto`
headers are believed. The default is `127.0.0.1,::1`; `--domain`, `--behind-proxy` and
`--tunnel-token` widen it to Docker's private ranges (the app port is then loopback-only). The
password rate limit is keyed on the resulting client IP. Never trust addresses that untrusted
clients can connect from.

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

- Change the admin or agent password: edit `.env`, then run `scripts/docker-start.sh restart`
  (or `sudo systemctl restart posthouse`). Existing OAuth connections and tokens keep working;
  revoke them in the UI if needed.
- Forgot the admin password: read it from `.env` (`scripts/docker-start.sh passwords`) or from
  `data/admin-password.txt` if it was auto-generated.
- Workspace passwords are set or removed in the admin UI (Workspace → Settings).

## Health and logs

- `GET /healthz` returns `{"ok": true}`. The Docker image has a built-in healthcheck.
- Logs: `scripts/docker-start.sh logs` or `journalctl -u posthouse -f`.
- Activity (who did what) is in the admin UI → Activity. File history is in Workspace → History.
