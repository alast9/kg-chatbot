"""
capabilities/snowflake.py — Snowflake capability
=================================================
Supports two execution backends, switchable via SNOWFLAKE_BACKEND env var:

  SNOWFLAKE_BACKEND=api   (default, current)
    POST /api/v2/statements — all column types returned as strings, reliable.

  SNOWFLAKE_BACKEND=mcp   (future — blocked on Snowflake bug)
    Native Cortex MCP server (SYSTEM_EXECUTE_SQL type).
    Currently broken: returns "Error parsing response" for ALL queries including
    SELECT 1, even when authenticated correctly. Failures do not appear in
    ACCOUNT_USAGE.QUERY_HISTORY — they fail inside Snowflake's MCP protocol
    layer before reaching the query engine. Bug filed; no ETA.

    Switch to MCP when:
      □  SELECT 1 via mcp__snowflake__sql_exec returns a result (not "Error
         parsing response") — this is the single acid test
      □  NUMBER and TIMESTAMP_NTZ columns are serialized correctly in results
      □  Results appear in ACCOUNT_USAGE.QUERY_HISTORY (confirms engine reach)

    MCP advantages worth the switch when fixed:
      - No per-user token management (Snowflake handles auth natively)
      - Streaming result sets (no JSON size limit)
      - Future Cortex features (Analyst, Search) exposed as additional tools

Auth:
  Per-user Entra ID SSO (External OAuth). User logs in via Entra ID once;
  the chatbot silently exchanges the refresh_token for a Snowflake-scoped token
  (api://<SF_APP_ID>/session:role:DEMO_READER).
  Snowflake accepts this as External OAuth. Token is auto-refreshed on expiry.

Schema discovery:
  At login, INFORMATION_SCHEMA.COLUMNS is queried to build a per-user schema
  cache injected into the LLM system prompt.

  Scalability note: with O(100) tables the schema string fits in LLM context
  comfortably (~10k tokens). Above ~200 tables, consider:
    - Filtering to a curated table list (WHERE table_name IN (...))
    - Two-stage retrieval: embed column descriptions, retrieve top-K per query
    - Domain-grouped schema with table comments to reduce LLM confusion
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from .base import Capability

log = logging.getLogger("cap.snowflake")

# ── Config ────────────────────────────────────────────────────────────────────

SNOWFLAKE_ACCOUNT   = os.getenv("SNOWFLAKE_ACCOUNT",   "XJSKMFC-WQC92044")
SNOWFLAKE_DATABASE  = "DEMO_DB"
SNOWFLAKE_SCHEMA    = "PUBLIC"
SNOWFLAKE_WAREHOUSE = os.getenv("SNOWFLAKE_WAREHOUSE",  "COMPUTE_WH")
SNOWFLAKE_ROLE      = os.getenv("SNOWFLAKE_ROLE",       "DEMO_READER")

# "api" (default) or "mcp" (future — blocked on Snowflake bug, see module docstring)
SNOWFLAKE_BACKEND   = os.getenv("SNOWFLAKE_BACKEND", "api").lower()

_BASE_URL   = f"https://{SNOWFLAKE_ACCOUNT}.snowflakecomputing.com"
SQL_API_URL = f"{_BASE_URL}/api/v2/statements"

# MCP endpoint — update to the active MCP server name when switching to mcp backend
MCP_SERVER_NAME = os.getenv("SNOWFLAKE_MCP_SERVER", "DEMO_MCP_SERVER")
MCP_URL = (
    f"{_BASE_URL}/api/v2/databases/{SNOWFLAKE_DATABASE}"
    f"/schemas/{SNOWFLAKE_SCHEMA}/mcp-servers/{MCP_SERVER_NAME}"
)

_SCHEMA_QUERY = (
    f"SELECT table_name, column_name, data_type, comment "
    f"FROM {SNOWFLAKE_DATABASE}.INFORMATION_SCHEMA.COLUMNS "
    f"WHERE table_schema = '{SNOWFLAKE_SCHEMA}' "
    f"ORDER BY table_name, ordinal_position"
)


# ── Backend: SQL REST API ─────────────────────────────────────────────────────

def _exec_via_api(token: str, sql: str, timeout: int = 60) -> dict:
    """
    Execute SQL via Snowflake SQL REST API v2 (/api/v2/statements).
    All column types are serialized as strings — no type-serialization issues.
    Returns {"rows": [...], "row_count": N, "columns": [...]}
    """
    body = {
        "statement":  sql,
        "timeout":    timeout,
        "database":   SNOWFLAKE_DATABASE,
        "schema":     SNOWFLAKE_SCHEMA,
        "warehouse":  SNOWFLAKE_WAREHOUSE,
        "role":       SNOWFLAKE_ROLE,
        "parameters": {"MULTI_STATEMENT_COUNT": "0"},
    }
    req = urllib.request.Request(
        SQL_API_URL,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type":  "application/json",
            "Accept":        "application/json",
            "Authorization": f"Bearer {token}",
            "X-Snowflake-Authorization-Token-Type": "OAUTH",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout + 10) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body_text = ""
        try: body_text = e.read().decode()[:500]
        except Exception: pass
        raise RuntimeError(f"Snowflake SQL API HTTP {e.code}: {body_text}")

    meta      = resp.get("resultSetMetaData", {})
    col_names = [c["name"] for c in meta.get("rowType", [])]
    rows      = [dict(zip(col_names, row)) for row in resp.get("data", [])]
    return {
        "rows":      rows,
        "row_count": meta.get("numRows", len(rows)),
        "columns":   col_names,
    }


# ── Backend: Snowflake native MCP ─────────────────────────────────────────────
#
# TO ENABLE: set SNOWFLAKE_BACKEND=mcp
#
# STATUS: blocked — Snowflake MCP server (SYSTEM_EXECUTE_SQL type) returns
# "Error parsing response" for all queries. Even SELECT 1 fails. Failures do
# not appear in ACCOUNT_USAGE.QUERY_HISTORY (fail before reaching query engine).
#
# ACID TEST before switching:
#   curl -X POST <MCP_URL>/sse \
#     -H "Authorization: Bearer <oauth_token>" \
#     -H "X-Snowflake-Authorization-Token-Type: OAUTH" \
#     -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"sql_exec","arguments":{"sql":"SELECT 1 AS n"}},"id":1}'
#   Expected: result with rows; Actual (broken): "Error parsing response"

def _exec_via_mcp(token: str, sql: str, timeout: int = 60) -> dict:
    """
    Execute SQL via Snowflake native Cortex MCP server.
    NOTE: Currently broken (see module docstring). Do not use until Snowflake fixes the bug.
    """
    import uuid
    body = {
        "jsonrpc": "2.0",
        "method":  "tools/call",
        "params":  {"name": "sql_exec", "arguments": {"sql": sql}},
        "id":      str(uuid.uuid4()),
    }
    req = urllib.request.Request(
        f"{MCP_URL}/sse",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type":  "application/json",
            "Accept":        "application/json",
            "Authorization": f"Bearer {token}",
            "X-Snowflake-Authorization-Token-Type": "OAUTH",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout + 10) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body_text = ""
        try: body_text = e.read().decode()[:500]
        except Exception: pass
        raise RuntimeError(f"Snowflake MCP HTTP {e.code}: {body_text}")

    # Parse MCP JSON-RPC response → rows
    result = resp.get("result", {})
    content = result.get("content", [])
    if not content:
        error = resp.get("error", {})
        raise RuntimeError(f"MCP error: {error.get('message', 'empty response')}")
    text = content[0].get("text", "")
    try:
        data = json.loads(text)
        rows = data if isinstance(data, list) else data.get("rows", [])
        return {"rows": rows, "row_count": len(rows),
                "columns": list(rows[0].keys()) if rows else []}
    except Exception as e:
        raise RuntimeError(f"MCP result parse error: {e} — raw: {text[:200]}")


# ── Unified executor ──────────────────────────────────────────────────────────

def _exec_sql(token: str, sql: str, timeout: int = 60) -> dict:
    """Route to the configured backend (api or mcp)."""
    if SNOWFLAKE_BACKEND == "mcp":
        return _exec_via_mcp(token, sql, timeout)
    return _exec_via_api(token, sql, timeout)


# ── Capability ────────────────────────────────────────────────────────────────

class SnowflakeCapability(Capability):
    """
    Snowflake natural language + SQL capability.
    Backend is selected by SNOWFLAKE_BACKEND env var (default: api).
    Schema is discovered per-user at login and cached for the LLM system prompt.
    """
    name = "snowflake"
    description = (
        f"SQL queries against Snowflake {SNOWFLAKE_DATABASE}.{SNOWFLAKE_SCHEMA} "
        f"(backend={SNOWFLAKE_BACKEND})"
    )

    def __init__(self):
        self._user_id: str = ""
        self._sf_token: str = ""
        self._sf_token_expires_at: float = 0.0
        self._sf_refresh_token: str = ""
        self._schema_cache: dict[str, str] = {}   # user_id → formatted schema string
        self._lock = threading.Lock()
        log.info("[Snowflake] backend=%s", SNOWFLAKE_BACKEND)

    def set_user_id(self, user_id: str) -> None:
        with self._lock:
            self._user_id = user_id
            has_token = bool(self._sf_token)
        if user_id and has_token and user_id not in self._schema_cache:
            threading.Thread(target=self._discover_schema, args=(user_id,), daemon=True).start()

    def set_snowflake_token(self, token: str, expires_at: float, refresh_token: str = "") -> None:
        with self._lock:
            self._sf_token = token
            self._sf_token_expires_at = expires_at
            self._sf_refresh_token = refresh_token
            user_id = self._user_id
        if user_id and token and user_id not in self._schema_cache:
            threading.Thread(target=self._discover_schema, args=(user_id,), daemon=True).start()

    # ── Schema discovery ──────────────────────────────────────────────────────

    def _discover_schema(self, user_id: str) -> None:
        """
        Always uses SQL REST API regardless of SNOWFLAKE_BACKEND — schema discovery
        doesn't benefit from MCP and the API is more reliable for metadata queries.
        """
        try:
            token  = self._token()
            result = _exec_via_api(token, _SCHEMA_QUERY)
            rows   = result.get("rows", [])
            if not rows:
                log.warning("[Snowflake] schema discovery returned no rows for %s", user_id[:8])
                return

            tables: dict[str, list[str]] = {}
            for row in rows:
                tbl = row.get("TABLE_NAME", "?")
                col = row.get("COLUMN_NAME", "?")
                typ = row.get("DATA_TYPE", "")
                cmt = row.get("COMMENT") or ""
                entry = f"    {col}: {typ}" + (f"  -- {cmt}" if cmt else "")
                tables.setdefault(tbl, []).append(entry)

            lines = [
                f"SNOWFLAKE — {SNOWFLAKE_DATABASE}.{SNOWFLAKE_SCHEMA} (schema for this user's role)",
                f"  Fully-qualify tables as {SNOWFLAKE_DATABASE}.{SNOWFLAKE_SCHEMA}.<table>\n",
            ]
            for tbl, cols in tables.items():
                lines.append(f"  TABLE: {SNOWFLAKE_DATABASE}.{SNOWFLAKE_SCHEMA}.{tbl}")
                lines.extend(cols)
                lines.append("")

            schema_str = "\n".join(lines)
            with self._lock:
                self._schema_cache[user_id] = schema_str
            log.info("[Snowflake] schema cached for user %s: %d tables", user_id[:8], len(tables))

        except Exception as e:
            log.warning("[Snowflake] schema discovery failed for %s: %s", user_id[:8], e)

    # ── Startup ───────────────────────────────────────────────────────────────

    def startup_check(self) -> tuple[bool, str]:
        if not os.getenv("SNOWFLAKE_OAUTH_CLIENT_ID"):
            return False, "SNOWFLAKE_OAUTH_CLIENT_ID not set"
        return True, (
            f"Snowflake ready — backend={SNOWFLAKE_BACKEND} "
            f"account={SNOWFLAKE_ACCOUNT} db={SNOWFLAKE_DATABASE}.{SNOWFLAKE_SCHEMA}"
        )

    # ── Capability interface ──────────────────────────────────────────────────

    def static_context(self) -> str:
        return ""

    def system_fragment(self) -> str:
        return (
            f"SNOWFLAKE DATA QUERIES (backend={SNOWFLAKE_BACKEND})\n"
            f"  Use snowflake_run_sql to execute SELECT queries against {SNOWFLAKE_DATABASE}.{SNOWFLAKE_SCHEMA}.\n"
            f"  The schema (tables and columns) is provided in context below — use it to write correct SQL.\n"
            f"  If schema context is missing, run: SELECT table_name, column_name, data_type "
            f"FROM {SNOWFLAKE_DATABASE}.INFORMATION_SCHEMA.COLUMNS "
            f"WHERE table_schema = '{SNOWFLAKE_SCHEMA}' ORDER BY table_name, ordinal_position\n"
            f"  Only SELECT is supported. Do NOT use SHOW, DESCRIBE, or DDL.\n"
            f"  Fully-qualify tables: {SNOWFLAKE_DATABASE}.{SNOWFLAKE_SCHEMA}.<table>\n"
            f"  If a tool call returns a token/auth error, tell the user to log out and back in."
        )

    def dynamic_fragment(self, user_id: str) -> str:
        with self._lock:
            return self._schema_cache.get(user_id, "")

    def tools(self) -> list[dict]:
        return [
            {
                "name": "snowflake_run_sql",
                "description": (
                    f"Execute a SQL SELECT against Snowflake {SNOWFLAKE_DATABASE}.{SNOWFLAKE_SCHEMA}. "
                    "Runs under the authenticated user's Snowflake role. "
                    "Use the schema context to write correct queries."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "sql":         {"type": "string", "description": "SQL SELECT to execute"},
                        "description": {"type": "string", "description": "What this query computes"},
                    },
                    "required": ["sql"],
                },
            },
        ]

    # ── Tool execution ────────────────────────────────────────────────────────

    def handle_tool(self, name: str, inputs: dict) -> Any:
        if name == "snowflake_run_sql":
            return self._run_sql(inputs["sql"], inputs.get("description", ""))
        return None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _token(self) -> str:
        from auth.oidc import exchange_snowflake_token
        with self._lock:
            token      = self._sf_token
            expires_at = self._sf_token_expires_at
            refresh_tk = self._sf_refresh_token
        if not token:
            raise RuntimeError(
                "Snowflake not connected — your session may have expired, please log out and back in")
        if time.time() >= expires_at and refresh_tk:
            token, new_exp = exchange_snowflake_token(refresh_tk)
            if token:
                with self._lock:
                    self._sf_token = token
                    self._sf_token_expires_at = new_exp
            else:
                raise RuntimeError("Snowflake token refresh failed — please log out and back in")
        return token

    def _run_sql(self, sql: str, description: str = "") -> dict:
        if description:
            log.info("[Snowflake/%s] SQL (%s): %s", SNOWFLAKE_BACKEND, description, sql[:120])
        try:
            token  = self._token()
            result = _exec_sql(token, sql)
            result["executed_sql"] = sql
            result["backend"] = SNOWFLAKE_BACKEND
            return result
        except RuntimeError as e:
            log.error("[Snowflake/%s] SQL failed: %s", SNOWFLAKE_BACKEND, e)
            return {"error": str(e), "executed_sql": sql, "backend": SNOWFLAKE_BACKEND}
