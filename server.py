"""MySQL MCP Server.

Two operating modes, selected via MCP_TRANSPORT:

1. ``stdio`` (default) — local subprocess mode. The client launches this
   process; no network, no auth. Suitable for Claude Desktop, Cursor, etc.
   when the database is local.

2. ``streamable-http`` — hosted web-service mode. The server validates a
   bearer token (forwarded by the client as the user's IDE/SSO token)
   against an OIDC-compliant IdP via its JWKS endpoint. Clients connect
   over HTTPS at a public URL.

In both modes, mutating tools are still gated by ``MYSQL_ALLOW_WRITE`` /
``MYSQL_ALLOW_DDL``. Authentication is an additional gate, not a
replacement for those flags.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import contextmanager
from typing import Any

import jwt
import mysql.connector
from jwt import PyJWKClient
from mysql.connector import pooling
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mysql-mcp")

# ---------- Configuration ----------------------------------------------------

def _bool_env(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "y", "on"}

DB_CONFIG: dict[str, Any] = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "database": os.getenv("MYSQL_DATABASE") or None,
    "charset": os.getenv("MYSQL_CHARSET", "utf8mb4"),
    "use_pure": True,
    # autocommit=True keeps each query in its own transaction and releases
    # locks promptly. Explicit conn.commit()/rollback() in write_query still
    # work — they're just no-ops when autocommit is on.
    "autocommit": _bool_env("MYSQL_AUTOCOMMIT", True),
}

# ---- SSL / TLS --------------------------------------------------------------
# mysql-connector-python attempts TLS by default, but managed databases
# (RDS, Aurora, Azure DB for MySQL, etc.) often require it explicitly and
# reject plaintext fallback with "Access denied". Make the intent explicit.
SSL_DISABLED = _bool_env("MYSQL_SSL_DISABLED", False)
if SSL_DISABLED:
    DB_CONFIG["ssl_disabled"] = True
else:
    # ssl_disabled=False forces TLS — no silent plaintext fallback.
    DB_CONFIG["ssl_disabled"] = False
    if ca := os.getenv("MYSQL_SSL_CA"):
        DB_CONFIG["ssl_ca"] = ca
    if cert := os.getenv("MYSQL_SSL_CERT"):
        DB_CONFIG["ssl_cert"] = cert
    if key := os.getenv("MYSQL_SSL_KEY"):
        DB_CONFIG["ssl_key"] = key
    # Default to *not* verifying — matches the pymysql `ssl:{ssl:{}}` idiom
    # commonly used with managed DBs where the CA isn't local. Flip these on
    # for production once you have the CA bundle deployed.
    DB_CONFIG["ssl_verify_cert"] = _bool_env("MYSQL_SSL_VERIFY_CERT", False)
    DB_CONFIG["ssl_verify_identity"] = _bool_env("MYSQL_SSL_VERIFY_IDENTITY", False)

ALLOW_WRITE = _bool_env("MYSQL_ALLOW_WRITE", False)
ALLOW_DDL = _bool_env("MYSQL_ALLOW_DDL", False)
MAX_ROWS = int(os.getenv("MYSQL_MAX_ROWS", "1000"))
POOL_SIZE = int(os.getenv("MYSQL_POOL_SIZE", "5"))

TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()
if TRANSPORT not in {"stdio", "streamable-http"}:
    raise SystemExit(f"Unsupported MCP_TRANSPORT={TRANSPORT!r}")

# ---------- Connection pool --------------------------------------------------

_pool: pooling.MySQLConnectionPool | None = None


def get_pool() -> pooling.MySQLConnectionPool:
    global _pool
    if _pool is None:
        cfg = {k: v for k, v in DB_CONFIG.items() if v is not None}
        _pool = pooling.MySQLConnectionPool(
            pool_name="mcp_pool",
            pool_size=POOL_SIZE,
            pool_reset_session=True,
            **cfg,
        )
        log.info("Initialized MySQL pool host=%s db=%s size=%d",
                 cfg.get("host"), cfg.get("database"), POOL_SIZE)
    return _pool


@contextmanager
def get_cursor(dictionary: bool = True):
    conn = get_pool().get_connection()
    try:
        cur = conn.cursor(dictionary=dictionary)
        try:
            yield conn, cur
        finally:
            cur.close()
    finally:
        conn.close()


# ---------- SQL classification ----------------------------------------------

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"--[^\n]*")

READ_VERBS = {"SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "WITH"}
DDL_VERBS = {"CREATE", "ALTER", "DROP", "TRUNCATE", "RENAME"}
WRITE_VERBS = {"INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE"}


def classify(sql: str) -> str:
    cleaned = _BLOCK_COMMENT.sub(" ", sql)
    cleaned = _LINE_COMMENT.sub(" ", cleaned).strip()
    if not cleaned:
        return "other"
    first = cleaned.split(None, 1)[0].upper()
    if first in READ_VERBS:
        return "read"
    if first in WRITE_VERBS:
        return "write"
    if first in DDL_VERBS:
        return "ddl"
    return "other"


def has_multiple_statements(sql: str) -> bool:
    cleaned = _BLOCK_COMMENT.sub(" ", sql)
    cleaned = _LINE_COMMENT.sub(" ", cleaned)
    stripped = cleaned.rstrip().rstrip(";").rstrip()
    return ";" in stripped


def _json_safe(value: Any) -> Any:
    import datetime
    import decimal
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return str(value)
    return value


def _normalize_rows(rows: list[dict]) -> list[dict]:
    return [{k: _json_safe(v) for k, v in row.items()} for row in rows]


# ---------- Auth (HTTP mode only) -------------------------------------------

def _build_token_verifier_and_settings():
    """Build the JWKS-based TokenVerifier and AuthSettings.

    Imports happen here so stdio mode doesn't need PyJWT or pydantic auth bits.
    """
    from mcp.server.auth.provider import AccessToken, TokenVerifier
    from mcp.server.auth.settings import AuthSettings
    from pydantic import AnyHttpUrl

    jwks_url = os.environ["OIDC_JWKS_URL"]
    issuer = os.environ["OIDC_ISSUER"]
    audience = os.environ["OIDC_AUDIENCE"]
    algorithms = [a.strip() for a in os.getenv("OIDC_ALGORITHMS", "RS256").split(",") if a.strip()]
    required_scopes = [s for s in os.getenv("OIDC_REQUIRED_SCOPES", "").split() if s]
    public_url = os.environ["MCP_PUBLIC_URL"]

    # PyJWKClient handles JWKS fetching, key caching, and key rotation. Keys
    # are refreshed on cache misses (e.g. when the IdP rotates signing keys).
    jwks = PyJWKClient(jwks_url, cache_keys=True, lifespan=600)

    class JWKSTokenVerifier(TokenVerifier):
        async def verify_token(self, token: str) -> AccessToken | None:
            try:
                # PyJWKClient is sync — offload to a thread so we don't
                # block the event loop on a JWKS cache miss.
                signing_key = await asyncio.to_thread(
                    lambda: jwks.get_signing_key_from_jwt(token).key
                )
                claims = jwt.decode(
                    token,
                    signing_key,
                    algorithms=algorithms,
                    audience=audience,
                    issuer=issuer,
                    options={"require": ["exp", "iat"]},
                )
            except jwt.PyJWTError as e:
                log.warning("token verification failed: %s", e)
                return None
            except Exception as e:  # PyJWKClient errors etc.
                log.warning("JWKS / signing key error: %s", e)
                return None

            # Extract scopes from either OAuth2 'scope' (space-delimited string)
            # or OIDC 'scp' (string or array).
            scope_claim = claims.get("scope") or claims.get("scp") or ""
            if isinstance(scope_claim, str):
                scopes = scope_claim.split()
            elif isinstance(scope_claim, list):
                scopes = list(scope_claim)
            else:
                scopes = []

            client_id = (
                claims.get("sub")
                or claims.get("email")
                or claims.get("preferred_username")
                or "unknown"
            )
            log.info("auth ok user=%s scopes=%s", client_id, scopes)

            return AccessToken(
                token=token,
                client_id=client_id,
                scopes=scopes,
                expires_at=claims.get("exp"),
            )

    settings = AuthSettings(
        issuer_url=AnyHttpUrl(issuer),
        resource_server_url=AnyHttpUrl(public_url),
        required_scopes=required_scopes or None,
    )
    return JWKSTokenVerifier(), settings


# ---------- MCP server -------------------------------------------------------

if TRANSPORT == "streamable-http":
    verifier, auth_settings = _build_token_verifier_and_settings()
    mcp = FastMCP(
        "mysql-mcp-server",
        host=os.getenv("MCP_BIND_HOST", "0.0.0.0"),
        port=int(os.getenv("MCP_BIND_PORT", "8000")),
        json_response=True,       # recommended for production
        stateless_http=True,      # scale horizontally without sticky sessions
        token_verifier=verifier,
        auth=auth_settings,
    )
else:
    mcp = FastMCP("mysql-mcp-server")

# Annotation presets — see https://modelcontextprotocol.io for the spec.
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
MUTATING = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    openWorldHint=False,
)


@mcp.tool(annotations=READ_ONLY)
def list_databases() -> str:
    """List all databases visible to the connected user."""
    with get_cursor() as (_, cur):
        cur.execute("SHOW DATABASES")
        rows = [next(iter(r.values())) for r in cur.fetchall()]
    return json.dumps({"databases": rows}, indent=2)


@mcp.tool(annotations=READ_ONLY)
def list_tables(database: str | None = None) -> str:
    """List tables. Uses the connected database unless one is specified."""
    with get_cursor() as (_, cur):
        if database:
            cur.execute(
                "SELECT TABLE_NAME, TABLE_TYPE, TABLE_ROWS "
                "FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s "
                "ORDER BY TABLE_NAME",
                (database,),
            )
        else:
            cur.execute(
                "SELECT TABLE_NAME, TABLE_TYPE, TABLE_ROWS "
                "FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() "
                "ORDER BY TABLE_NAME"
            )
        rows = _normalize_rows(cur.fetchall())
    return json.dumps({"tables": rows}, indent=2)


@mcp.tool(annotations=READ_ONLY)
def describe_table(table: str, database: str | None = None) -> str:
    """Return columns, types, keys, and indexes for a table."""
    with get_cursor() as (_, cur):
        if database:
            cur.execute(
                "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, "
                "COLUMN_DEFAULT, EXTRA, COLUMN_COMMENT "
                "FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                "ORDER BY ORDINAL_POSITION",
                (database, table),
            )
        else:
            cur.execute(
                "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, "
                "COLUMN_DEFAULT, EXTRA, COLUMN_COMMENT "
                "FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
                "ORDER BY ORDINAL_POSITION",
                (table,),
            )
        columns = _normalize_rows(cur.fetchall())

        schema_filter = "TABLE_SCHEMA = %s" if database else "TABLE_SCHEMA = DATABASE()"
        params = (database, table) if database else (table,)
        cur.execute(
            f"SELECT INDEX_NAME, NON_UNIQUE, COLUMN_NAME, SEQ_IN_INDEX "
            f"FROM information_schema.STATISTICS "
            f"WHERE {schema_filter} AND TABLE_NAME = %s "
            f"ORDER BY INDEX_NAME, SEQ_IN_INDEX",
            params,
        )
        indexes = _normalize_rows(cur.fetchall())

    if not columns:
        return json.dumps({"error": f"Table not found: {table}"}, indent=2)
    return json.dumps(
        {"table": table, "columns": columns, "indexes": indexes},
        indent=2,
    )


@mcp.tool(annotations=READ_ONLY)
def read_query(sql: str, params: list | None = None, limit: int | None = None) -> str:
    """Execute a read-only query (SELECT/SHOW/DESCRIBE/EXPLAIN/WITH)."""
    kind = classify(sql)
    if kind != "read":
        return json.dumps({"error": f"read_query only accepts read queries, got '{kind}'."})
    if has_multiple_statements(sql):
        return json.dumps({"error": "Multiple statements are not allowed in a single call."})

    cap = min(limit or MAX_ROWS, MAX_ROWS)
    with get_cursor() as (_, cur):
        cur.execute(sql, tuple(params) if params else None)
        rows = cur.fetchmany(cap)
        truncated = cur.fetchone() is not None
        rows = _normalize_rows(rows)
    return json.dumps(
        {"row_count": len(rows), "truncated": truncated, "rows": rows},
        indent=2,
    )


@mcp.tool(annotations=MUTATING)
def write_query(sql: str, params: list | None = None) -> str:
    """Execute an INSERT/UPDATE/DELETE/REPLACE.

    Disabled unless MYSQL_ALLOW_WRITE=true.
    """
    if not ALLOW_WRITE:
        return json.dumps({"error": "Writes are disabled. Set MYSQL_ALLOW_WRITE=true to enable."})
    kind = classify(sql)
    if kind != "write":
        return json.dumps({"error": f"write_query only accepts write statements, got '{kind}'."})
    if has_multiple_statements(sql):
        return json.dumps({"error": "Multiple statements are not allowed in a single call."})

    with get_cursor() as (conn, cur):
        try:
            cur.execute(sql, tuple(params) if params else None)
            conn.commit()
            return json.dumps({
                "rows_affected": cur.rowcount,
                "last_insert_id": cur.lastrowid,
            }, indent=2)
        except Exception:
            conn.rollback()
            raise


@mcp.tool(annotations=MUTATING)
def ddl_query(sql: str) -> str:
    """Execute a DDL statement (CREATE/ALTER/DROP/TRUNCATE/RENAME).

    Disabled unless MYSQL_ALLOW_DDL=true.
    """
    if not ALLOW_DDL:
        return json.dumps({"error": "DDL is disabled. Set MYSQL_ALLOW_DDL=true to enable."})
    kind = classify(sql)
    if kind != "ddl":
        return json.dumps({"error": f"ddl_query only accepts DDL statements, got '{kind}'."})
    if has_multiple_statements(sql):
        return json.dumps({"error": "Multiple statements are not allowed in a single call."})

    with get_cursor() as (conn, cur):
        cur.execute(sql)
        conn.commit()
        return json.dumps({"status": "ok"}, indent=2)


@mcp.tool(annotations=READ_ONLY)
def server_info() -> str:
    """Show server config flags and MySQL version."""
    with get_cursor() as (_, cur):
        cur.execute("SELECT VERSION() AS version, DATABASE() AS db, USER() AS user")
        row = _normalize_rows(cur.fetchall())[0]
    return json.dumps({
        "mysql": row,
        "config": {
            "host": DB_CONFIG["host"],
            "port": DB_CONFIG["port"],
            "database": DB_CONFIG["database"],
            "transport": TRANSPORT,
            "auth": "oidc-jwt" if TRANSPORT == "streamable-http" else "none",
            "allow_write": ALLOW_WRITE,
            "allow_ddl": ALLOW_DDL,
            "max_rows": MAX_ROWS,
            "pool_size": POOL_SIZE,
        },
    }, indent=2)


# ---------- Entry point ------------------------------------------------------

if __name__ == "__main__":
    try:
        with get_cursor() as (_, cur):
            cur.execute("SELECT 1")
            cur.fetchall()
        log.info("MySQL connection check passed.")
    except mysql.connector.Error as e:
        log.error("Failed to connect to MySQL: %s", e)
        raise SystemExit(1)

    if TRANSPORT == "streamable-http":
        log.info("Starting on %s:%s — streamable-http with OIDC bearer auth",
                 os.getenv("MCP_BIND_HOST", "0.0.0.0"),
                 os.getenv("MCP_BIND_PORT", "8000"))
        mcp.run(transport="streamable-http")
    else:
        log.info("Starting in stdio mode (no auth)")
        mcp.run()