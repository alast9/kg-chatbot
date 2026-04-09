"""
capabilities/dremio.py — Dremio capability via Azure AI Foundry Gateway + Auth0 SSO
====================================================================================
Architecture:
  chatbot tool call
    → DremioCapability.handle_tool()
      → AzureAIGatewayClient.call_tool()   (JSON-RPC 2.0 over HTTPS / MCP 2025-11-25)
        ← Auth0 Bearer token (client_credentials, auto-refreshed, 3LO)
        → Azure AI Foundry Agent Service Gateway
          → Dremio MCP server
            → Dremio Cloud SQL engine

The gateway speaks JSON-RPC 2.0 (MCP protocol):
  POST /mcp  {"jsonrpc":"2.0","method":"tools/call",
              "params":{"name":"RunSqlQuery","arguments":{"query":"SELECT..."}}}

The Auth0 token is fetched once and cached until 60s before expiry.
All tool calls reuse the cached token; 401 responses trigger a forced refresh.

For dremio_nl_query (natural language → SQL → result):
  Step 1: Claude on Azure AI generates SQL from question + schema context
  Step 2: call_tool("RunSqlQuery", {"query": sql}) via Azure AI Foundry gateway
"""
from __future__ import annotations

import logging
import os
from typing import Any

import openai

from .base import Capability
from auth.azure_ai_gateway import AzureAIGatewayClient, get_gateway_client
from auth.sso import EntraTokenManager, get_token_manager

log = logging.getLogger("cap.dremio")

# LLM on Azure AI (for dremio_nl_query SQL generation step only)
AZURE_AI_ENDPOINT = os.getenv("AZURE_AI_ENDPOINT", "")
AZURE_AI_API_KEY  = os.getenv("AZURE_AI_API_KEY",  "")
AZURE_AI_MODEL    = os.getenv("AZURE_AI_MODEL",    "DeepSeek-R1")

_llm_client = openai.OpenAI(
    base_url=AZURE_AI_ENDPOINT or None,
    api_key=AZURE_AI_API_KEY or "placeholder",
    default_query={"api-version": "2024-05-01-preview"},
)


class DremioCapability(Capability):
    """
    Dremio Cloud natural language query capability.
    All data access goes through the Azure AI Foundry Gateway (Auth0 SSO / 3LO).
    """
    name = "dremio"
    description = "Natural language queries against Dremio Cloud customer360 data (via Azure AI Foundry Gateway + Auth0)"

    # Pre-built schema catalog (discovered via MCP SearchTableAndViews earlier)
    SCHEMA_CATALOG = """\
DREMIO CLOUD — dremio_samples.customer360
  Gateway: Azure AI Foundry Agent Service (MCP 2025-11-25)
  Auth:    Auth0 client_credentials → Bearer token → Azure AI Foundry inbound auth (3LO)

  TABLE: "dremio_samples"."customer360"."customer"
    customer_id: VARCHAR  |  full_name: VARCHAR  |  address: VARCHAR
    zip: VARCHAR  |  state: VARCHAR  |  phone: VARCHAR  |  email: VARCHAR
    join_date: DATE  |  membership: VARCHAR  (values: gold, silver, bronze)
    Scale: 4.8M rows

  TABLE: "dremio_samples"."customer360"."orders"
    order_id: VARCHAR  |  customer_id: VARCHAR
    order_timestamp: TIMESTAMP  |  total_price: DOUBLE
    Scale: 177M rows

  TABLE: "dremio_samples"."customer360"."order_line_items"
    customer_id: VARCHAR  |  line_item_id: VARCHAR  |  order_id: VARCHAR
    order_timestamp: TIMESTAMP  |  product_id: VARCHAR  |  price: DOUBLE
    Scale: 540M+ rows

  TABLE: "dremio_samples"."customer360"."product"
    product_id: VARCHAR  |  name: VARCHAR  |  brand: VARCHAR
    description: VARCHAR  |  list_price: DOUBLE

  TABLE: "dremio_samples"."customer360"."reviews_and_returned_items"
    customer_id: VARCHAR  |  line_item_id: VARCHAR  |  product_id: VARCHAR
    rating: BIGINT (1-5)  |  returned: BOOLEAN

  KEY RELATIONSHIPS:
    customer.customer_id       → orders.customer_id
    orders.order_id            → order_line_items.order_id
    order_line_items.product_id → product.product_id
    order_line_items.line_item_id → reviews_and_returned_items.line_item_id

  DREMIO SQL RULES: quote reserved words with double quotes — "count", "month", "day", "year", "table"
  Use DATE_TRUNC for time bucketing. Use OVER() for window functions."""

    def __init__(self,
                 gateway: AzureAIGatewayClient | None = None,
                 token_manager: EntraTokenManager | None = None):
        self._gateway    = gateway or get_gateway_client()
        self._auth       = token_manager or get_token_manager()
        self._user_token: str = ""   # set per-request from user's OAuth session
        self._lock       = __import__("threading").Lock()

    def set_user_token(self, token: str) -> None:
        """Inject user's delegated access_token for Dremio data access control."""
        with self._lock:
            self._user_token = token

    # ── Startup ───────────────────────────────────────────────────────────────

    def startup_check(self) -> tuple[bool, str]:
        auth_ok, auth_msg = self._auth.startup_check()
        if not auth_ok:
            return False, auth_msg
        gw_ok, gw_msg = self._gateway.startup_check()
        return gw_ok, f"{auth_msg} | {gw_msg}"

    # ── Capability interface ──────────────────────────────────────────────────

    def static_context(self) -> str:
        return self.SCHEMA_CATALOG

    def system_fragment(self) -> str:
        return """\
DREMIO CLOUD DATA QUERIES (via Azure AI Foundry Gateway + Auth0 SSO / 3LO)
  Use dremio_nl_query for natural language questions about customers, orders, products, revenue, returns.
  Use dremio_run_sql when you have a specific SQL query to run directly.
  Use dremio_search_tables to discover available tables or verify schemas.
  Always quote Dremio SQL reserved words: "count", "month", "day", "year", "table".
  Table prefix: "dremio_samples"."customer360"."<table_name>"."""

    def tools(self) -> list[dict]:
        return [
            {
                "name": "dremio_nl_query",
                "description": (
                    "Answer a natural language question by generating and running SQL "
                    "against Dremio customer360 via AgentCore. Use for: 'top 5 customers by revenue', "
                    "'monthly order trend', 'most returned products', 'average order value by state', "
                    "'membership tier breakdown'. Claude generates the SQL; gateway executes it."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "Natural language question"},
                    },
                    "required": ["question"],
                },
            },
            {
                "name": "dremio_run_sql",
                "description": (
                    "Execute a specific SQL SELECT via the AgentCore gateway. "
                    "Quote reserved words: \"count\", \"month\", \"day\"."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query":       {"type": "string", "description": "SQL SELECT query"},
                        "description": {"type": "string", "description": "What this computes"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "dremio_search_tables",
                "description": "Semantic search to discover Dremio tables and views by topic.",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
            {
                "name": "dremio_get_lineage",
                "description": "Get data lineage for a Dremio table or view.",
                "input_schema": {
                    "type": "object",
                    "properties": {"table_name": {"type": "string"}},
                    "required": ["table_name"],
                },
            },
            {
                "name": "dremio_system_tables",
                "description": "List useful Dremio system tables for cluster analysis.",
                "input_schema": {"type": "object", "properties": {}, "required": []},
            },
        ]

    # ── Tool execution ────────────────────────────────────────────────────────

    def handle_tool(self, name: str, inputs: dict) -> Any:
        if name == "dremio_nl_query":
            return self._nl_query(inputs["question"])
        elif name == "dremio_run_sql":
            return self._run_sql(inputs["query"], inputs.get("description", ""))
        elif name == "dremio_search_tables":
            return self._call("SearchTableAndViews", {"query": inputs["query"]})
        elif name == "dremio_get_lineage":
            return self._call("GetTableOrViewLineage", {"table_name": inputs["table_name"]})
        elif name == "dremio_system_tables":
            return self._call("GetUsefulSystemTableNames", {})
        return None

    # ── Gateway helpers ───────────────────────────────────────────────────────

    def _call(self, mcp_tool: str, arguments: dict) -> dict:
        """Call a Dremio MCP tool via the AgentCore gateway.
        Uses user's delegated token when set (for per-user data access control),
        falls back to M2M token for background/system calls.
        """
        with self._lock:
            user_tok = self._user_token
        try:
            if user_tok:
                # Temporarily override the gateway's auth with user token
                result = self._gateway.call_tool_with_token(mcp_tool, arguments, user_tok)
            else:
                result = self._gateway.call_tool(mcp_tool, arguments)
            log.info("[Dremio] %s → %s", mcp_tool, str(result)[:80])
            return result
        except RuntimeError as e:
            log.error("[Dremio] %s failed: %s", mcp_tool, e)
            return {"error": str(e)}

    def _run_sql(self, query: str, description: str = "") -> dict:
        """Execute SQL via the gateway RunSqlQuery tool."""
        if description:
            log.info("[Dremio] SQL (%s): %s", description, query[:80])
        result = self._call("RunSqlQuery", {"query": query})
        result["executed_sql"] = query
        return result

    def _nl_query(self, question: str) -> dict:
        """
        Natural language → SQL (via Claude on Azure AI) → result (via Azure AI Foundry gateway).

        Step 1: Claude on Azure AI generates SQL from question + pre-built schema catalog.
                Pure LLM call — no gateway involved.
        Step 2: Execute via Azure AI Foundry gateway with Auth0 auth (3LO).
                Token auto-refreshed on expiry.
        """
        # ── Step 1: Generate SQL via Claude on Azure AI ───────────────────────
        prompt = f"""\
Given this Dremio schema:
{self.SCHEMA_CATALOG}

Write a SQL query to answer: "{question}"

Rules:
- Quote reserved words: "count", "month", "day", "year", "table"
- Full table names: "dremio_samples"."customer360"."<table>"
- Return ONLY the SQL query. No explanation, no markdown fences.
- Add LIMIT 50 unless the question asks for aggregations or full counts."""

        try:
            azure_resp = _llm_client.chat.completions.create(
                model=AZURE_AI_MODEL,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            return {"error": f"SQL generation failed: {e}", "question": question}

        content = azure_resp.choices[0].message.content or ""
        # Strip DeepSeek-R1 thinking blocks
        import re as _re
        content = _re.sub(r"<think>.*?</think>", "", content, flags=_re.DOTALL)
        sql = content.strip()

        # Strip accidental markdown fences
        if "```" in sql:
            sql = "\n".join(
                l for l in sql.split("\n") if not l.strip().startswith("```")
            ).strip()

        log.info("[Dremio] NL→SQL for %r: %s", question[:50], sql[:120])

        # ── Step 2: Execute via AgentCore gateway ─────────────────────────────
        result = self._run_sql(sql, description=f"NL: {question[:60]}")
        result["question"]      = question
        result["generated_sql"] = sql
        return result
