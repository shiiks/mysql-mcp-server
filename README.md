# MySQL MCP Server

A Model Context Protocol server that exposes a MySQL database to LLM clients (Claude Desktop, Claude Code, etc.). Read-only by default; writes and DDL are opt-in via environment variables.

## Tools

| Tool | Purpose | Requires |
|---|---|---|
| `server_info` | Show MySQL version and current server config | — |
| `list_databases` | List databases visible to the user | — |
| `list_tables` | List tables in the current (or specified) database | — |
| `describe_table` | Columns, types, keys, and indexes for a table | — |
| `read_query` | Run `SELECT` / `SHOW` / `DESCRIBE` / `EXPLAIN` / `WITH` | — |
| `write_query` | Run `INSERT` / `UPDATE` / `DELETE` / `REPLACE` | `MYSQL_ALLOW_WRITE=true` |
| `ddl_query` | Run `CREATE` / `ALTER` / `DROP` / `TRUNCATE` / `RENAME` | `MYSQL_ALLOW_DDL=true` |

Statements are classified by their leading verb (after stripping comments), parameterized via `%s` placeholders, and rejected if they contain multiple statements.

## Install

```bash
git clone <this repo> mysql-mcp-server && cd mysql-mcp-server
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # edit credentials
```

## Run standalone

```bash
set -a; source .env; set +a
python server.py
```

The process speaks MCP over stdio. It will exit immediately if it can't connect to MySQL.

## Client setup

The server speaks MCP over stdio, so any MCP client that can launch a subprocess works. Replace `/absolute/path/to/...` with real paths in every snippet, and prefer absolute paths to your venv's Python interpreter so the client doesn't need to know about activation.

> **A note on passwords.** All snippets below show the password inline for clarity. For anything beyond local experimentation, use the secret-prompt mechanism your client supports (VS Code's `inputs`, OS keychain, etc.) instead of committing credentials.

### Claude Desktop

Edit `claude_desktop_config.json` (Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "mysql": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/server.py"],
      "env": {
        "MYSQL_HOST": "127.0.0.1",
        "MYSQL_PORT": "3306",
        "MYSQL_USER": "readonly_user",
        "MYSQL_PASSWORD": "secret",
        "MYSQL_DATABASE": "mydb",
        "MYSQL_ALLOW_WRITE": "false",
        "MYSQL_MAX_ROWS": "500"
      }
    }
  }
}
```

Restart Claude Desktop. Tools appear in the connections panel.

### VS Code

VS Code reads MCP config from `.vscode/mcp.json` (workspace) or your user profile (Command Palette → **MCP: Open User Configuration**). Note the top-level key is `servers`, **not** `mcpServers`.

```json
{
  "inputs": [
    {
      "type": "promptString",
      "id": "mysql-password",
      "description": "MySQL password",
      "password": true
    }
  ],
  "servers": {
    "mysql": {
      "type": "stdio",
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/server.py"],
      "env": {
        "MYSQL_HOST": "127.0.0.1",
        "MYSQL_PORT": "3306",
        "MYSQL_USER": "readonly_user",
        "MYSQL_PASSWORD": "${input:mysql-password}",
        "MYSQL_DATABASE": "mydb"
      }
    }
  }
}
```

VS Code prompts for the password the first time the server starts and stores it securely. Tools show up in agent mode. You can also use **MCP: Add Server** from the Command Palette for a guided flow.

### Cursor

Cursor uses `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` in a project root. Project config wins when both define the same server.

```json
{
  "mcpServers": {
    "mysql": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/server.py"],
      "env": {
        "MYSQL_HOST": "127.0.0.1",
        "MYSQL_PORT": "3306",
        "MYSQL_USER": "readonly_user",
        "MYSQL_PASSWORD": "secret",
        "MYSQL_DATABASE": "mydb"
      }
    }
  }
}
```

Open **Cursor Settings → Tools & MCP** to confirm the server shows a green dot and the tools list is populated. Cursor caps active tools across all servers at ~40, so disable any you aren't using.

### Windsurf

Windsurf (Cascade) uses one global config file:

- macOS / Linux: `~/.codeium/windsurf/mcp_config.json`
- Windows: `%USERPROFILE%\.codeium\windsurf\mcp_config.json`

Open it via **Windsurf Settings → Cascade → View raw config**, or click the MCP icon in the Cascade panel and choose **Configure**.

```json
{
  "mcpServers": {
    "mysql": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/server.py"],
      "env": {
        "MYSQL_HOST": "127.0.0.1",
        "MYSQL_PORT": "3306",
        "MYSQL_USER": "readonly_user",
        "MYSQL_PASSWORD": "secret",
        "MYSQL_DATABASE": "mydb"
      }
    }
  }
}
```

After saving, click **Refresh** in the MCP toolbar — Windsurf doesn't auto-reload the config. There's no per-project scope and no env var interpolation (values are passed through verbatim).

### Codex

Codex (CLI and IDE extension) uses **TOML**, not JSON, at `~/.codex/config.toml` (or `.codex/config.toml` for trusted projects). The table name is `mcp_servers` with an underscore — `mcp-servers` is silently ignored.

```toml
[mcp_servers.mysql]
command = "/absolute/path/to/.venv/bin/python"
args = ["/absolute/path/to/server.py"]
env = { MYSQL_HOST = "127.0.0.1", MYSQL_PORT = "3306", MYSQL_USER = "readonly_user", MYSQL_PASSWORD = "secret", MYSQL_DATABASE = "mydb" }
```

Or register it via the CLI without hand-editing TOML:

```bash
codex mcp add mysql \
  --env MYSQL_HOST=127.0.0.1 \
  --env MYSQL_PORT=3306 \
  --env MYSQL_USER=readonly_user \
  --env MYSQL_PASSWORD=secret \
  --env MYSQL_DATABASE=mydb \
  -- /absolute/path/to/.venv/bin/python /absolute/path/to/server.py
```

Run `codex mcp list` to confirm. The same config file is shared between the CLI and the IDE extension, so a syntax error breaks both — validate with `codex mcp list` after edits.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `MYSQL_HOST` | `127.0.0.1` | |
| `MYSQL_PORT` | `3306` | |
| `MYSQL_USER` | `root` | |
| `MYSQL_PASSWORD` | *(empty)* | |
| `MYSQL_DATABASE` | *(none)* | If unset, queries must qualify table names |
| `MYSQL_CHARSET` | `utf8mb4` | |
| `MYSQL_ALLOW_WRITE` | `false` | Enables `write_query` |
| `MYSQL_ALLOW_DDL` | `false` | Enables `ddl_query` |
| `MYSQL_MAX_ROWS` | `1000` | Hard cap returned by `read_query` |
| `MYSQL_POOL_SIZE` | `5` | Connections in the pool |
| `MCP_TRANSPORT` | `stdio` | `stdio` \| `sse` \| `streamable-http` |
| `MCP_HTTP_HOST` | `0.0.0.0` | Bind address (HTTP transports only) |
| `MCP_HTTP_PORT` | `8080` | Bind port (HTTP transports only) |
| `IDE_AUTH_REQUIRED` | `true` | Set `false` to disable bearer-token auth (dev only) |
| `IDE_AUTH_ISSUER` | *(none)* | OIDC issuer URL — required for HTTP transports |
| `IDE_AUTH_AUDIENCE` | *(none)* | Expected `aud` claim — required for HTTP transports |
| `IDE_AUTH_JWKS_URL` | *(derived)* | Defaults to `<issuer>/.well-known/jwks.json` |

## Running as an external (remote) server

In addition to stdio mode (where each client launches its own subprocess), the server can run as a long-lived HTTP service that many clients connect to over the network. Use this mode when:

- MySQL credentials should live on the server, not be distributed to every user
- You want one central audit log of all queries
- You're publishing to a shared MCP catalog where users authenticate via SSO rather than database accounts

### Transports

Set `MCP_TRANSPORT` to one of:

- `stdio` (default) — client launches the server as a subprocess
- `sse` — Server-Sent Events HTTP transport, broadest client compatibility
- `streamable-http` — newer Streamable HTTP transport per current MCP spec

### IDE authentication

When running over HTTP, every request must carry `Authorization: Bearer <jwt>`. The server validates the JWT against the configured OIDC issuer's JWKS, checks `aud`, `iss`, and expiry, then logs the authenticated identity alongside each query. The IDE handles forwarding the user's SSO token; the server only validates.

Required environment:

```
IDE_AUTH_ISSUER=https://sso.example.com/
IDE_AUTH_AUDIENCE=mysql-mcp
# IDE_AUTH_JWKS_URL is optional — defaults to <issuer>/.well-known/jwks.json
```

`GET /healthz` is exempt from auth so orchestrator probes (Kubernetes, Docker, ELB) can reach it.

> **Authentication ≠ authorization.** A valid token only gets the caller through the door. *What* they're allowed to query is governed by the database user the server connects with — keep that user least-privileged. "The user authenticated" and "the user is allowed to read the salaries table" are different decisions.

### Docker

```bash
docker build -t mysql-mcp .
docker run --rm -p 8080:8080 \
  -e MYSQL_HOST=db.internal \
  -e MYSQL_USER=mcp_readonly \
  -e MYSQL_PASSWORD=secret \
  -e MYSQL_DATABASE=analytics \
  -e IDE_AUTH_ISSUER=https://sso.example.com/ \
  -e IDE_AUTH_AUDIENCE=mysql-mcp \
  mysql-mcp
```

Clients then connect to `https://your-host/sse/` (SSE) or `https://your-host/mcp/` (Streamable HTTP). Confirm the exact mount path from the server logs at startup.

## Security recommendations

- **Use a dedicated database user** with the minimum privileges the LLM should have. For analytics, `GRANT SELECT` is enough. Don't reuse a root or app account.
- **Keep writes off** unless you actually need them. Consider running two instances — one read-only, one with writes — and gate the writeable one behind a separate config.
- **Bind a specific database** via `MYSQL_DATABASE` so the LLM can't wander into other schemas.
- **Network**: prefer `127.0.0.1` or a private network. If you must use a remote host, require TLS at the MySQL level.
- The classifier is a guardrail, not a sandbox. Defense in depth is the database user's grants.

## Example session

```
> server_info()
{"mysql": {"version": "8.0.36", "db": "mydb", "user": "readonly@localhost"}, ...}

> list_tables()
{"tables": [{"TABLE_NAME": "orders", "TABLE_TYPE": "BASE TABLE", "TABLE_ROWS": 12034}, ...]}

> describe_table(table="orders")
{"table": "orders", "columns": [...], "indexes": [...]}

> read_query(sql="SELECT status, COUNT(*) c FROM orders GROUP BY status")
{"row_count": 4, "truncated": false, "rows": [...]}

> read_query(sql="SELECT * FROM orders WHERE id = %s", params=[42])
{"row_count": 1, "truncated": false, "rows": [{"id": 42, ...}]}
```

## Notes

- Decimals, dates, timedeltas, and bytes are converted to JSON-friendly strings.
- Each `write_query` runs in its own transaction and rolls back on error. DDL is generally not transactional in MySQL.
- The `pool_reset_session=True` setting clears session state between calls so one tool invocation can't leak temp tables or session vars to the next.