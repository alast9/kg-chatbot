"""
capabilities/dremio.py — Dremio Cloud capability via REST API + OAuth
======================================================================
Architecture:
  chatbot tool call
    → DremioCapability.handle_tool()
      → _exec_sql()  (POST /v0/projects/{project_id}/sql + poll)
        ← Per-user Dremio OAuth token (from /auth/dremio/connect flow)
           OR service-account PAT (DREMIO_PAT env var, fallback)

Auth:
  Two modes (automatic fallback):
    1. Per-user OAuth — user clicks "Connect Dremio" in UI → Dremio OAuth login
       → token stored in UserSession.dremio_token
    2. Service account — DREMIO_PAT env var (PAT), shared by all users

OAuth endpoints (Dremio Cloud org-level OAuth App):
  Authorize:  https://app.dremio.cloud/oauth2/authorize
  Token:      https://api.dremio.cloud/v0/projects/{project_id}/oauth2/token
  Redirect:   <APP_BASE_URL>/auth/dremio/connect   (configured in Dremio OAuth app)

SQL execution:
  1. POST /v0/projects/{project_id}/sql  → {"id": job_id}
  2. Poll GET /v0/projects/{project_id}/job/{job_id} until jobState == COMPLETED
  3. GET /v0/projects/{project_id}/job/{job_id}/results  → rows

Note: Engine cold-start takes ~40s on Dremio Cloud serverless.
All columns returned as JSON-native types (INTEGER, VARCHAR, DOUBLE, etc.)

To add SSO (like Snowflake): configure Entra ID as SAML/OIDC IdP in Dremio
Cloud org settings, then exchange the Entra ID token for a Dremio token silently
at chatbot login (eliminating the explicit /auth/dremio/connect step).
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

log = logging.getLogger("cap.dremio")

DREMIO_PROJECT_ID = os.getenv("DREMIO_PROJECT_ID", "dea2a74c-2f8a-4eef-8d40-c87db48d79ff")
DREMIO_PAT        = os.getenv("DREMIO_PAT", "")   # service-account fallback
DREMIO_BASE_URL   = f"https://api.dremio.cloud/v0/projects/{DREMIO_PROJECT_ID}"

_JOB_POLL_INTERVAL = 2    # seconds between job status polls
_JOB_TIMEOUT       = 120  # seconds before giving up


# ── REST API helpers ──────────────────────────────────────────────────────────

def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }


def _get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers=_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode()[:400]
        except Exception: pass
        raise RuntimeError(f"Dremio GET {e.code}: {body}")


def _post(url: str, token: str, body: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers=_headers(token),
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body_text = ""
        try: body_text = e.read().decode()[:400]
        except Exception: pass
        raise RuntimeError(f"Dremio POST {e.code}: {body_text}")


def _exec_sql(token: str, sql: str, timeout: int = _JOB_TIMEOUT) -> dict:
    """
    Submit SQL job, poll until complete, return rows.
    Handles Dremio Cloud serverless cold-start (~40s for first query).
    Returns {"rows": [...], "row_count": N, "columns": [...], "job_id": "..."}
    """
    # Submit job
    resp = _post(f"{DREMIO_BASE_URL}/sql", token, {"sql": sql})
    job_id = resp.get("id")
    if not job_id:
        raise RuntimeError(f"Dremio SQL submission failed: {resp}")

    log.info("[Dremio] Job submitted: %s  SQL: %s", job_id, sql[:80])

    # Poll for completion
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(_JOB_POLL_INTERVAL)
        status = _get(f"{DREMIO_BASE_URL}/job/{job_id}", token)
        state = status.get("jobState", "")
        log.debug("[Dremio] Job %s state: %s", job_id, state)

        if state == "COMPLETED":
            break
        if state in ("FAILED", "CANCELED", "INVALID_STATE"):
            err = status.get("errorMessage", state)
            raise RuntimeError(f"Dremio job {state}: {err}")
        # ENGINE_START / QUEUED / PLANNING / RUNNING — keep polling

    else:
        raise RuntimeError(f"Dremio job {job_id} timed out after {timeout}s")

    # Fetch results
    results = _get(f"{DREMIO_BASE_URL}/job/{job_id}/results", token)
    rows     = results.get("rows", [])
    schema   = results.get("schema", [])
    columns  = [c["name"] for c in schema]

    return {
        "rows":      rows,
        "row_count": results.get("rowCount", len(rows)),
        "columns":   columns,
        "job_id":    job_id,
    }


# ── Capability ────────────────────────────────────────────────────────────────

class DremioCapability(Capability):
    """
    Dremio Cloud natural language + SQL capability.
    Uses per-user OAuth token when available, falls back to service-account PAT.
    """
    name = "dremio"
    description = "SQL queries against Dremio Cloud customer360 dataset"

    # Schema catalog — injected into LLM system prompt
    SCHEMA_CATALOG = """\
DREMIO CLOUD — dremio_samples.customer360
  Project: dea2a74c-2f8a-4eef-8d40-c87db48d79ff
  Auth:    per-user OAuth (preferred) or service-account PAT (fallback)

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
    customer.customer_id        → orders.customer_id
    orders.order_id             → order_line_items.order_id
    order_line_items.product_id → product.product_id
    order_line_items.line_item_id → reviews_and_returned_items.line_item_id

  DREMIO SQL RULES:
    - Quote reserved words: "count", "month", "day", "year", "table"
    - Full table path: "dremio_samples"."customer360"."<table>"
    - Use DATE_TRUNC for time bucketing
    - Use OVER() for window functions"""

    def __init__(self):
        self._dremio_token:            str   = ""
        self._dremio_token_expires_at: float = 0.0
        self._lock = threading.Lock()

    def set_dremio_token(self, token: str, expires_at: float) -> None:
        """Inject per-user Dremio OAuth token (called from web.py per WebSocket)."""
        with self._lock:
            self._dremio_token            = token
            self._dremio_token_expires_at = expires_at

    # ── Startup ───────────────────────────────────────────────────────────────

    def startup_check(self) -> tuple[bool, str]:
        if not DREMIO_PAT and not os.getenv("DREMIO_OAUTH_CLIENT_ID"):
            return False, "DREMIO_PAT or DREMIO_OAUTH_CLIENT_ID must be set"
        mode = "PAT" if DREMIO_PAT else "OAuth"
        return True, f"Dremio Cloud ready (project={DREMIO_PROJECT_ID}, auth={mode})"

    # ── Capability interface ──────────────────────────────────────────────────

    def static_context(self) -> str:
        return self.SCHEMA_CATALOG

    def dynamic_fragment(self, user_id: str) -> str:
        return ""

    def system_fragment(self) -> str:
        return """\
DREMIO CLOUD DATA QUERIES
  Use dremio_run_sql to query customer360 data (customers, orders, products, reviews).
  The schema is provided in context — use exact table and column names.
  Always quote Dremio reserved words with double quotes: "count", "month", "day", "year", "table".
  Full table path: "dremio_samples"."customer360"."<table>".
  Note: first query may take ~40s while Dremio engine warms up — inform the user if slow.
  If a query returns an auth error, ask the user to connect Dremio at /auth/dremio/connect."""

    def tools(self) -> list[dict]:
        return [
            {
                "name": "dremio_run_sql",
                "description": (
                    "Execute a SQL SELECT against Dremio Cloud customer360. "
                    "Use for: top customers by revenue, monthly order trends, "
                    "most returned products, average order value by state, membership breakdown. "
                    "Note: first query may take ~40s while the Dremio engine starts."
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

    def handle_tool(self, name: str, inputs: dict) -> Any:
        if name == "dremio_run_sql":
            return self._run_sql(inputs["sql"], inputs.get("description", ""))
        return None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _token(self) -> str:
        """Return the best available token: per-user OAuth > service PAT."""
        with self._lock:
            tok = self._dremio_token
            exp = self._dremio_token_expires_at

        # Per-user token valid
        if tok and time.time() < exp:
            return tok

        # Fall back to PAT
        if DREMIO_PAT:
            return DREMIO_PAT

        raise RuntimeError(
            "Dremio not connected — please visit /auth/dremio/connect to authorise "
            "or set DREMIO_PAT for service-account access")

    def _run_sql(self, sql: str, description: str = "") -> dict:
        if description:
            log.info("[Dremio] SQL (%s): %s", description, sql[:120])
        try:
            token  = self._token()
            result = _exec_sql(token, sql)
            result["executed_sql"] = sql
            return result
        except RuntimeError as e:
            log.error("[Dremio] SQL failed: %s", e)
            return {"error": str(e), "executed_sql": sql}
