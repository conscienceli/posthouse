# Posthouse 驿站

**为你所有 AI 智能体提供共享、带版本历史的记忆。** Claude、ChatGPT、Gemini、Claude Code、
Codex、Gemini CLI、Cursor 等都能读写同一个项目。任何一个智能体新开对话，调用一次就能跟上项目的最新进度。

*名字取自古代的驿站：信使在驿站交接文书，换人换马继续上路。在这里，每个智能体在工作区交接，下一个接着干。*

[English README](README.md)

![管理界面中的工作区文件](docs/images/files.png)

更多截图：[修改历史](docs/images/history.png) · [OAuth 登录页](docs/images/oauth.png)

## 为什么需要它

网页版聊天应用无法挂载文件共享，也不能登录 WebDAV，而且每个应用的记忆都是各自分开的。
Posthouse（驿站）给每个项目一个**工作区**：就是你服务器上的一个普通文件夹。
智能体可以通过三种方式访问：

- **MCP**：Claude、ChatGPT、Gemini 和各类命令行智能体把它添加为远程 MCP 连接器。
  服务器自带 OAuth 登录页，在页面上输入密码即可。
- **REST + Skill**：带命令行的智能体（Claude Cowork、Claude Code、Codex……）使用仓库自带的
  Skill、`posthouse.py` 或 `curl`。
- **管理界面**：你在浏览器里浏览、编辑、上传、恢复旧版本，并管理访问权限。

```
 Claude.ai ─┐                    ┌──────────── Posthouse ─────────────┐
 ChatGPT  ──┤  MCP (OAuth)       │  /mcp     MCP 服务（Streamable HTTP）           │
 Gemini   ──┤ ─────────────────▶ │  /oauth   OAuth 2.1（密码登录页）               │──▶ data/workspaces/<名称>/
 Claude Code┤                    │  /api     REST API（含 /api/docs）              │      AGENTS.md  STATUS.md
 Codex ─────┤  REST / Skill      │  /        管理界面                              │      DECISIONS.md journal/
 你 ────────┘ ─────────────────▶ │  git 历史 · SQLite 索引 · 活动日志              │      memory/ files/  (.git)
                                 └─────────────────────────────────────────────────┘
```

## 功能

- **普通文件，完整历史。** 每个工作区既是磁盘上的一个目录，也是一个独立的 git 仓库。
  每次修改都是一次提交，作者记为做出修改的那个智能体，所以可以查看旧版本、对比差异并恢复。
  删除的工作区会先进入回收站。
- **一次调用即可上手。** `start_session` 一次返回项目指南、当前状态、最近决策、最新交接记录、
  文件列表和最近修改。
- **交接约定。** 新建的工作区自带 `AGENTS.md`、`STATUS.md`、`DECISIONS.md`、`journal/`、
  `memory/` 和 `files/`。`log_progress` 写入一条带日期的交接记录，同时可以更新 `STATUS.md`。
- **安全的并发编辑。** 写入时可以带上读取时拿到的版本号：如果文件在你读取之后被改过，写入会失败。
  `edit_file`（精确替换片段）和 `append_file`（追加）可以避免整文件覆盖。
- **兼容现有的 MCP 客户端。** 同一个地址同时支持无状态的 **2026-07-28** 协议
  （`server/discover`、每个请求自带 `_meta`）和较早基于 `initialize` 握手的版本
  （2024-11-05 至 2025-11-25）。已用官方 MCP Python SDK v1 和 v2 测试。
- **网页版应用能接受的认证方式。** 内置 OAuth 2.1 授权服务器：服务发现、动态客户端注册、
  客户端 ID 元数据文档、PKCE（S256）以及会轮换的刷新令牌。命令行工具和脚本也可以用
  HTTP Basic、可撤销的访问令牌或带令牌的秘密地址。
- **三级密码。** 管理员密码、智能体密码，以及每个工作区可选的独立密码（见下文）。
- **管理界面。** 工作区列表、文件浏览与编辑、上传、历史与恢复、访问令牌、已连接应用
  （可撤销 OAuth 授权）、活动日志、回收站和连接说明。界面默认英文，可在下拉菜单里切换语言
  （English、简体中文）。添加更多语言见[多语言](#多语言)。
- **部署简单。** 单个 Python 进程加 SQLite，不依赖其他外部服务。可以用 Docker 运行
  （可选用 Caddy 自动配置 HTTPS），也可以直接在任意 Linux 服务器上运行。

## 快速开始

### 使用 Docker（推荐）

```bash
git clone https://github.com/conscienceli/posthouse.git
cd posthouse

# 按你的服务器情况选一行（网页版 Claude/ChatGPT/Gemini 需要公网 HTTPS）：
./scripts/docker-start.sh --domain posthouse.example.com                # 80/443 端口空闲：自带 HTTPS
./scripts/docker-start.sh --behind-proxy https://posthouse.example.com  # 前面已有 Nginx 或 Cloudflare
./scripts/docker-start.sh --behind-proxy https://posthouse.example.com --tunnel-token <令牌>  # Cloudflare Tunnel
./scripts/docker-start.sh                                               # 测试用：HTTP，端口 8080
```

脚本会创建 `.env` 并随机生成管理员密码和智能体密码，然后构建镜像、启动容器，最后打印访问地址和密码。
其他命令：`logs`、`status`、`restart`、`stop`、`update`、`passwords`。数据保存在 `./data`。
接入现有 Nginx、以及 Cloudflare（Tunnel、Full (strict)、Flexible）的分步说明见 [docs/deploy.md](docs/deploy.md)。

### 不使用 Docker

```bash
./scripts/install.sh                 # 创建 Python 虚拟环境、安装依赖、生成 .env
./scripts/run.sh                     # 前台运行
sudo ./scripts/install.sh --systemd --behind-proxy https://posthouse.example.com   # systemd 服务，放在 Nginx 后面
```

环境要求：Linux、Python 3.10+、`git`（可选，用于历史记录）。Nginx 配置示例：
[`deploy/nginx.conf.example`](deploy/nginx.conf.example)、[`deploy/nginx-cloudflare.conf.example`](deploy/nginx-cloudflare.conf.example)。

> **网页版应用需要公网 HTTPS。** Claude、ChatGPT、Gemini 都是从它们自己的云端发起连接，
> 所以服务器必须能通过 443 端口的 `https://…` 访问。详见 [docs/deploy.md](docs/deploy.md)，
> 其中也有中国大陆服务器的注意事项。

## 权限级别

| 级别 | 凭据 | 能做什么 |
|---|---|---|
| **管理员** | `POSTHOUSE_ADMIN_PASSWORD` | 一切操作：管理界面、删除/重命名工作区、设置密码、令牌、撤销应用 |
| **智能体** | `POSTHOUSE_AGENT_PASSWORD` | 列出并**新建**工作区；读写**所有**工作区的文件 |
| **工作区** | 该工作区自己的密码（可选） | **只能**读写该工作区的文件 |

提交凭据的方式：

- **OAuth 登录页。** 网页版应用和大多数 MCP 客户端会打开这个页面，你在页面上输入密码。
  “工作区”留空即授予该密码的完整权限；填写工作区名称则这次连接只能访问该工作区
  （使用管理员或智能体密码时同样有效）。
- **HTTP Basic。** 用户名是 `agent`、`admin` 或工作区名称，再加上对应的密码
  （适用于 Skill、curl、命令行智能体和 Claude 请求头）。
- **访问令牌**（`ph_…`）。在管理界面创建，用法是 `Authorization: Bearer ph_…`；
  也可以用秘密地址 `https://…/mcp/t/ph_…`，适用于只能填写地址的应用。
  令牌可随时撤销，也可以限定在单个工作区。

## 连接你的智能体

MCP 地址是 `https://<你的域名>/mcp`。管理界面的“连接智能体”页面会显示这个地址，并带有复制按钮。

| 智能体 | 连接方式 |
|---|---|
| **Claude**（网页、桌面、手机、Cowork） | 设置 → 连接器 → *添加自定义连接器* → 粘贴地址 → 用密码登录 |
| **ChatGPT**（网页） | 设置 → 安全 → 开启*开发者模式*，然后在 应用/插件 → *+* → 粘贴地址 → OAuth → 输入密码。每个新对话里都要手动选择它。需要付费套餐；能否写入取决于套餐（beta）。 |
| **Gemini**（网页） | 设置 → 已连接的应用 → *添加自定义应用*（目前仅限美国的个人账号） |
| **Claude Code** | `claude mcp add --transport http posthouse https://posthouse.example.com/mcp`，然后用 `/mcp` 登录。也可以加上 `--header "Authorization: Bearer ph_…"` |
| **Codex CLI** | `codex mcp add posthouse --url https://posthouse.example.com/mcp`，然后 `codex mcp login posthouse`。也可以在 `~/.codex/config.toml` 中设置 `bearer_token_env_var` |
| **Gemini CLI** | `gemini mcp add --transport http posthouse https://posthouse.example.com/mcp`，然后 `/mcp auth posthouse`。也可以在 `settings.json` 中配置 `"httpUrl"` + `"headers"` |
| **任何带命令行的智能体** | [Skill](skills/posthouse/SKILL.md) / `posthouse.py` / `curl`，配合 Basic 认证或令牌 |
| **只能填写地址的应用** | 在管理界面创建令牌，使用 `https://…/mcp/t/ph_…` |

各平台的分步说明和常见问题见 [docs/mcp.md](docs/mcp.md)。新对话的开场提示词可以这样写：

> 你可以使用“Posthouse”连接器。我们的项目在工作区“my-project”中。
> 请先调用 start_session(workspace="my-project")，阅读 AGENTS.md 和 STATUS.md。
> 工作中保持 STATUS.md 为最新，结束前调用 log_progress。

## MCP 工具

| 工具 | 用途 |
|---|---|
| `list_workspaces` | 列出你能访问的工作区 |
| `create_workspace` | 按标准结构新建工作区（智能体/管理员） |
| `start_session` | 加载指南、状态、决策、交接记录、文件列表和最近修改 |
| `list_files` · `read_file` | 浏览与读取（支持 `offset`/`limit`，或指定旧的 `revision`） |
| `write_file` · `edit_file` · `append_file` | 写入（可带 `expected_version`）、精确替换片段、追加 |
| `move_file` · `delete_file` | 移动/重命名与删除（可从历史中恢复） |
| `search` · `fetch` | 全文搜索；按 id 获取（格式兼容 ChatGPT 深度研究） |
| `log_progress` | 在 `journal/YYYY-MM-DD.md` 写交接记录，可同时更新 `STATUS.md` |
| `history` | 谁在什么时候改了什么 |

## Skill 与命令行工具

[`skills/posthouse`](skills/posthouse) 是一个现成的智能体 Skill，包含
[`SKILL.md`](skills/posthouse/SKILL.md) 和 `scripts/posthouse.py`（一个只依赖 Python 标准库的命令行工具）。

```bash
export POSTHOUSE_URL=https://posthouse.example.com POSTHOUSE_USER=agent POSTHOUSE_PASSWORD=… POSTHOUSE_AGENT="Claude Code"
python3 posthouse.py start my-project
python3 posthouse.py read my-project STATUS.md
python3 posthouse.py log my-project --summary "完成第二节初稿" --next "审阅第三节"
```

`./scripts/package-skill.sh` 会生成 `dist/posthouse-skill.zip`，可以上传到 Claude。
在 Claude、Claude Code、Codex、Gemini CLI 中的用法见 [docs/skill.md](docs/skill.md)。

## REST API

管理界面和 Skill 的所有操作都通过 `/api` 完成。交互式文档在 `/api/docs`，摘要见 [docs/api.md](docs/api.md)。
适合用 curl 调用的原始文件接口：

```bash
curl -u agent:$PW https://posthouse.example.com/api/workspaces/my-project/raw/STATUS.md
curl -u agent:$PW -X PUT --data-binary @STATUS.md -H "If-Match: <版本号>" \
     https://posthouse.example.com/api/workspaces/my-project/raw/STATUS.md
```

## 配置

所有设置都来自环境变量或 `.env`，完整列表见 [`.env.example`](.env.example)。

| 变量 | 默认值 | 含义 |
|---|---|---|
| `POSTHOUSE_ADMIN_PASSWORD` | 自动生成 | 管理员密码（未设置时自动生成并保存到 `data/admin-password.txt`） |
| `POSTHOUSE_AGENT_PASSWORD` | 空（禁用） | 智能体密码 |
| `POSTHOUSE_PUBLIC_URL` | 根据请求推断 | 公网访问地址，如 `https://posthouse.example.com`（在反向代理后面时请设置） |
| `POSTHOUSE_DATA_DIR` | `./data`（Docker 中为 `/data`） | 工作区、回收站和 SQLite 数据库所在目录 |
| `POSTHOUSE_HOST` / `POSTHOUSE_PORT` | `0.0.0.0` / `8080` | 监听地址（不用 Docker 时） |
| `POSTHOUSE_DOMAIN` | 空 | 设置后启用自带的 Caddy HTTPS（Docker） |
| `POSTHOUSE_HOST_PORT` / `POSTHOUSE_BIND` | `8080` / `0.0.0.0` | 对外端口和绑定地址（Docker） |
| `POSTHOUSE_MAX_FILE_MB` | `10` | 单个文件大小上限 |
| `POSTHOUSE_GIT` | `true` | 用 git 记录每次修改 |
| `POSTHOUSE_ACCESS_TOKEN_TTL` | `3600` | OAuth 访问令牌有效期（秒） |
| `POSTHOUSE_REFRESH_TOKEN_DAYS` | `180` | OAuth 刷新令牌有效期（天，每次使用后轮换） |
| `POSTHOUSE_SESSION_HOURS` | `12` | 管理界面登录有效期 |
| `POSTHOUSE_LOGIN_MAX_FAILURES` / `POSTHOUSE_LOGIN_WINDOW_SECONDS` | `10` / `600` | 每个客户端 IP 的密码尝试限制 |
| `POSTHOUSE_TRUSTED_PROXIES` | `127.0.0.1,::1` | 允许设置 `X-Forwarded-*` 的代理（使用 `--domain` 时会自动为自带的 Caddy 设置） |

## 安全建议

- 始终通过 HTTPS 提供服务，并使用足够长的随机密码（启动脚本会自动生成）。
- 给智能体用**智能体密码**、**工作区密码**或**令牌**，不要给管理员密码。
  不再使用的 OAuth 连接和令牌，请在管理界面撤销。
- 一个智能体写入的内容会被其他智能体读到。自带的说明会要求智能体把文件内容当作数据，而不是指令；
  有 git 历史，任何修改都可以撤回。
- 密码错误次数按 IP 限制。管理界面使用 `HttpOnly`/`SameSite=Strict` Cookie 并加了 CSRF 请求头校验。
  文件永远不会以可执行的 HTML 形式返回。
- 如有需要，可以在防火墙上只放行各平台公布的出口 IP 段（见 [docs/deploy.md](docs/deploy.md)）。
  请定期备份 `data/` 目录。

## 多语言

界面文字在 [`posthouse/static/i18n/`](posthouse/static/i18n) 目录。添加一种语言的步骤：把 `en.json` 复制为
`<语言代码>.json`（如 `ja.json`、`fr.json`），翻译其中的值，再在 `languages.json` 里加一行
`{"code": "<语言代码>", "name": "<语言本地名称>"}`。管理界面的语言下拉菜单和 OAuth 登录页
（根据浏览器的 `Accept-Language` 自动选择）都使用这些文件。缺失的条目会自动用英文显示。

## 开发

```bash
pip install -r requirements-dev.txt
pytest                      # 单元测试与集成测试（REST、MCP、OAuth、CLI）
python -m posthouse --port 8080   # 若存在 .env 会自动读取
```

代码结构：`posthouse/storage.py`（工作区、文件、git）、`posthouse/auth.py`（密码、令牌）、`posthouse/oauth.py`（授权服务器）、
`posthouse/mcp_server.py`（MCP）、`posthouse/api.py`（REST）、`posthouse/static/`（管理界面，无需构建）。

## 许可证

[MIT](LICENSE)
