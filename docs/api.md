# REST API

Base path: `/api`. Interactive OpenAPI docs: `/api/docs`. All responses are JSON unless noted.

## Authentication

Send one of these:

- `Authorization: Basic base64(user:password)`, where user is `admin`, `agent` or a workspace name
- `Authorization: Bearer <token>`, with an access token `ph_…` or an OAuth access token
- the admin UI session cookie, which also needs `X-Posthouse-CSRF: 1` on non-GET requests

Optional: `X-Posthouse-Agent: <name>` sets the author recorded in history and activity.

Errors look like `{"error": "<code>", "message": "..."}` with status 400, 401, 403, 404, 409
(version conflict, also includes `current_version`), 413 (too large), 422 (validation) or 429
(rate limited).

## Endpoints

| Method & path | Who | Description |
|---|---|---|
| `GET /api/info` | anyone | Version, base URL, MCP URL, features |
| `GET /api/me` | anyone | `{authenticated, role, workspace, actor, via}` |
| `POST /api/login` `{password}` / `POST /api/logout` | — | Admin UI session cookie |
| `GET /api/workspaces` | any | Workspaces visible to the caller (with file count, size, timestamps) |
| `POST /api/workspaces` `{name, description?, password?, template?}` | agent, admin | Create |
| `GET /api/workspaces/{ws}` | any with access | Details, including `history_bytes` (size of the change history) |
| `PATCH /api/workspaces/{ws}` `{description?, password?, clear_password?, new_name?}` | admin (agent: description only) | Update |
| `DELETE /api/workspaces/{ws}` | admin | Move to trash |
| `POST /api/workspaces/{ws}/setup-message` `{expires_days?, lang?, agent?}` | admin | Creates a token for this workspace only and returns `{message, token_id, token_name, base_url, expires_at}`: a message to paste into a new Claude task |
| `POST /api/setup-message` `{expires_days?, lang?, agent?}` | admin | Same, with an agent token (all workspaces, can create new ones) for a task that picks or creates its workspace |
| `POST /api/workspaces/{ws}/compact` | admin | Compact the change history now (`git gc`; also runs automatically) → `{compacted, before_bytes, after_bytes}` |
| `GET /api/workspaces/{ws}/tree?path=&recursive=` | access | List entries |
| `GET /api/workspaces/{ws}/file?path=&offset=&limit=&revision=` | access | Read as JSON `{content, encoding, version, …}` |
| `PUT /api/workspaces/{ws}/file` `{path, content, encoding?, expected_version?, message?}` | access | Write (`encoding: base64` for binary; `expected_version: "new"` = must not exist) |
| `POST /api/workspaces/{ws}/edit` `{path, old_text, new_text, replace_all?, expected_version?}` | access | Exact-text replacement |
| `POST /api/workspaces/{ws}/append` `{path, content}` | access | Append |
| `POST /api/workspaces/{ws}/move` `{source, destination, overwrite?}` | access | Move/rename |
| `POST /api/workspaces/{ws}/mkdir` `{path}` | access | Create folder |
| `DELETE /api/workspaces/{ws}/file?path=&recursive=` | access | Delete (recoverable from history) |
| `GET /api/workspaces/{ws}/raw/{path}?revision=&download=` | access | Raw bytes; `X-Posthouse-Version` / `ETag` headers |
| `PUT /api/workspaces/{ws}/raw/{path}` | access | Raw body write; `If-Match: <version>`, `If-None-Match: *`, `X-Posthouse-Message`; `?append=true` appends. Writing identical content returns `unchanged: true` and records nothing |
| `POST /api/workspaces/{ws}/upload` (multipart `dir`, `files`) | access | Upload files |
| `GET /api/workspaces/{ws}/archive` | access | Zip of the workspace (without `.git`) |
| `GET /api/workspaces/{ws}/session?format=json\|md` | access | Session bundle (same as MCP `start_session`) |
| `POST /api/workspaces/{ws}/progress` `{summary, next_steps?, status?, agent?}` | access | Journal entry (+ STATUS.md) |
| `GET /api/workspaces/{ws}/history?path=&limit=` | access | Commits `{revision, short, author, time, message, files[]}` |
| `GET /api/search?q=&workspace=&limit=` | any | Full-text search across visible workspaces |
| `GET /api/activity?workspace=&limit=` | any | Activity log (admin sees everything) |
| `GET/POST /api/tokens`, `DELETE /api/tokens/{id}` | admin | Access tokens (`POST {name, role: agent\|workspace, workspace?, expires_days?}` returns the token once) |
| `GET /api/connections`, `DELETE /api/connections/{id}` | admin | OAuth grants (connected apps) |
| `GET /api/trash`, `POST /api/trash/{id}/restore {name?}`, `DELETE /api/trash/{id}` | admin | Trash |

Other endpoints: `GET /skill/posthouse-skill.zip` (the agent skill, public), `GET /healthz`, `POST /mcp` (MCP), `/oauth/*` and `/.well-known/*` (OAuth).

## Versions and conflicts

Every read returns a `version`, the first 16 hex characters of the SHA-256 of the file content.
Pass it back as `expected_version`, or as `If-Match` on raw writes. If someone changed the file in
the meantime, you get `409` with `current_version`; read the file again and re-apply your change.
