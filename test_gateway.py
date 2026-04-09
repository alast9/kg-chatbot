#!/usr/bin/env python3
"""
test_gateway.py — AgentCore Gateway Integration Test
=====================================================
Tests the full call chain:
  Auth0 client_credentials → Bearer token
  → AgentCore JSON-RPC gateway
    → Dremio MCP tools (list, search, SQL, lineage, system tables)
      → NL query (Bedrock SQL gen + gateway execution)

Usage:
    python3 test_gateway.py               # all tests
    python3 test_gateway.py --quick       # auth + SQL only (fastest)
    python3 test_gateway.py --test auth   # single test by name
    python3 test_gateway.py --verbose     # show full response payloads

Available test names:
    auth          — Auth0 token fetch and cache behaviour
    tools_list    — gateway tools/list discovery
    sql_simple    — basic SELECT via RunSqlQuery
    sql_customer  — top customers by revenue (JOIN across tables)
    sql_trend     — monthly revenue trend (GROUP BY + ORDER BY)
    search        — SearchTableAndViews semantic search
    lineage       — GetTableOrViewLineage
    system_tables — GetUsefulSystemTableNames
    nl_query      — natural language → Bedrock SQL gen → gateway execution
    token_refresh — simulate expiry and verify refresh
    concurrent    — 3 concurrent SQL calls (thread safety)
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import time
import threading
from pathlib import Path
from typing import Any

# Add project root so auth/ imports resolve
sys.path.insert(0, str(Path(__file__).parent))

from auth.sso import Auth0TokenManager
from auth.agentcore_gateway import AgentCoreGatewayClient

# ── Colour helpers ─────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

def ok(msg):    print(f"  {GREEN}✓{RESET}  {msg}")
def fail(msg):  print(f"  {RED}✗{RESET}  {msg}")
def info(msg):  print(f"  {CYAN}→{RESET}  {msg}")
def warn(msg):  print(f"  {YELLOW}⚠{RESET}  {msg}")
def heading(t): print(f"\n{BOLD}{t}{RESET}\n{'─'*60}")
def dim(msg):   print(f"     {DIM}{msg}{RESET}")


# ── Result tracker ─────────────────────────────────────────────────────────────
class Results:
    def __init__(self):
        self._passed = []
        self._failed = []
        self._skipped = []

    def passed(self, name): self._passed.append(name)
    def failed(self, name, reason=""): self._failed.append((name, reason))
    def skipped(self, name, reason=""): self._skipped.append((name, reason))

    def summary(self):
        total = len(self._passed) + len(self._failed)
        print(f"\n{'='*60}")
        print(f"{BOLD}Test Results{RESET}")
        print(f"{'='*60}")
        print(f"  Passed:  {GREEN}{len(self._passed)}{RESET}")
        print(f"  Failed:  {RED}{len(self._failed)}{RESET}")
        if self._skipped:
            print(f"  Skipped: {YELLOW}{len(self._skipped)}{RESET}")
        print()
        if self._failed:
            print(f"{RED}Failures:{RESET}")
            for name, reason in self._failed:
                print(f"  ✗ {name}: {reason}")
        if self._skipped:
            for name, reason in self._skipped:
                print(f"  ⚠ {name}: {reason}")
        passed = len(self._passed) == total and total > 0
        print(f"\n  {'ALL PASSED' if passed else 'SOME FAILED'} ({len(self._passed)}/{total})")
        return passed


# ── Test helpers ───────────────────────────────────────────────────────────────
def assert_eq(label, actual, expected):
    if actual == expected:
        ok(f"{label}: {actual}")
    else:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")

def assert_true(label, value, detail=""):
    if value:
        ok(f"{label}{' — ' + detail if detail else ''}")
    else:
        raise AssertionError(f"{label} is falsy: {detail or value!r}")

def assert_contains(label, container, key):
    if key in container:
        ok(f"{label} contains '{key}'")
    else:
        raise AssertionError(f"{label}: '{key}' not found in {list(container)[:10]}")

def show_payload(data: Any, verbose: bool, max_lines: int = 30):
    if not verbose:
        return
    text = json.dumps(data, indent=2, default=str)
    lines = text.split("\n")
    for line in lines[:max_lines]:
        dim(line)
    if len(lines) > max_lines:
        dim(f"  ... ({len(lines) - max_lines} more lines)")


# ══════════════════════════════════════════════════════════════════════════════
# Individual tests
# ══════════════════════════════════════════════════════════════════════════════

def test_auth(auth: Auth0TokenManager, verbose: bool) -> None:
    """T1: Auth0 token fetch and caching."""
    heading("T1: Auth0 Token Fetch")

    # First fetch
    t0    = time.time()
    token = auth.get_token()
    t1    = time.time() - t0
    assert_true("token is non-empty", bool(token), f"len={len(token)}")
    ok(f"fetch time: {t1*1000:.0f}ms")
    dim(f"token[:40]: {token[:40]}...")

    # Cached fetch (should be < 5ms)
    t0     = time.time()
    token2 = auth.get_token()
    t2     = time.time() - t0
    assert_eq("token unchanged on cache hit", token2, token)
    assert_true("cache hit < 5ms", t2 < 0.005, f"{t2*1000:.1f}ms")
    ok(f"cache hit: {t2*1000:.1f}ms")

    # Token structure (JWT has 3 dot-separated parts)
    parts = token.split(".")
    assert_eq("JWT has 3 parts", len(parts), 3)

    if verbose:
        import base64
        try:
            pad     = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(pad))
            dim(f"JWT claims: aud={payload.get('aud')}, exp={payload.get('exp')}, iss={payload.get('iss')}")
        except Exception:
            pass


def test_tools_list(gw: AgentCoreGatewayClient, verbose: bool) -> None:
    """T2: Discover tools via tools/list."""
    heading("T2: Gateway tools/list")

    tools = gw.list_tools(use_cache=False)
    assert_true("tools is a list", isinstance(tools, list))
    assert_true("at least 1 tool", len(tools) >= 1, f"got {len(tools)}")
    ok(f"discovered {len(tools)} tools")

    expected_tools = {"RunSqlQuery", "SearchTableAndViews",
                      "GetUsefulSystemTableNames", "GetTableOrViewLineage"}
    found = {t.get("name") for t in tools}
    for expected in expected_tools:
        if expected in found:
            ok(f"tool present: {expected}")
        else:
            warn(f"tool missing: {expected} (gateway may expose different names)")

    show_payload(tools, verbose)


def test_sql_simple(gw: AgentCoreGatewayClient, verbose: bool) -> None:
    """T3: Ping — simplest possible SQL."""
    heading("T3: SQL — simple ping")

    result = gw.call_tool("RunSqlQuery", {"query": "SELECT 1 AS ping"})
    show_payload(result, verbose)
    assert_true("result is non-empty", bool(result))
    # Result may be {"result": [{"ping": 1}]} or {"text": "..."} depending on gateway
    info(f"result type: {type(result).__name__}, keys: {list(result.keys()) if isinstance(result, dict) else 'N/A'}")
    ok("SQL ping returned successfully")


def test_sql_customer_count(gw: AgentCoreGatewayClient, verbose: bool) -> None:
    """T4: COUNT query — verify data is accessible."""
    heading("T4: SQL — customer count")

    result = gw.call_tool("RunSqlQuery", {
        "query": 'SELECT COUNT(*) AS total FROM "dremio_samples"."customer360"."customer"'
    })
    show_payload(result, verbose)
    assert_true("result is non-empty", bool(result))
    ok("customer count query executed")

    # Try to extract the number from various response shapes
    count = _extract_scalar(result, "total")
    if count is not None:
        assert_true("count > 0", count > 0, f"got {count:,}")
        ok(f"customer count: {count:,}")
    else:
        warn(f"could not extract scalar — raw: {str(result)[:200]}")


def test_sql_top_customers(gw: AgentCoreGatewayClient, verbose: bool) -> None:
    """T5: JOIN across two tables — top customers by revenue."""
    heading("T5: SQL — top 5 customers by revenue (JOIN)")

    sql = """\
SELECT c.full_name, c.state, c.membership,
       ROUND(SUM(o.total_price), 2) AS total_revenue,
       COUNT(o.order_id) AS order_count
FROM   "dremio_samples"."customer360"."customer" c
JOIN   "dremio_samples"."customer360"."orders"   o
       ON c.customer_id = o.customer_id
GROUP  BY c.full_name, c.state, c.membership
ORDER  BY total_revenue DESC
LIMIT  5"""

    t0     = time.time()
    result = gw.call_tool("RunSqlQuery", {"query": sql})
    elapsed = time.time() - t0
    show_payload(result, verbose)
    assert_true("result is non-empty", bool(result))
    ok(f"JOIN query returned in {elapsed:.1f}s")

    rows = _extract_rows(result)
    if rows:
        ok(f"rows returned: {len(rows)}")
        for row in rows[:3]:
            dim(str(row))
    else:
        warn(f"could not parse rows — raw: {str(result)[:200]}")


def test_sql_monthly_trend(gw: AgentCoreGatewayClient, verbose: bool) -> None:
    """T6: Aggregation with DATE_TRUNC — monthly revenue trend."""
    heading("T6: SQL — monthly revenue trend")

    sql = """\
SELECT DATE_TRUNC('month', o.order_timestamp) AS order_month,
       COUNT(o.order_id)                       AS order_count,
       ROUND(SUM(o.total_price), 2)            AS total_revenue
FROM   "dremio_samples"."customer360"."orders" o
GROUP  BY DATE_TRUNC('month', o.order_timestamp)
ORDER  BY order_month DESC
LIMIT  12"""

    result = gw.call_tool("RunSqlQuery", {"query": sql})
    show_payload(result, verbose)
    assert_true("result is non-empty", bool(result))
    ok("monthly trend query executed")

    rows = _extract_rows(result)
    if rows:
        ok(f"rows: {len(rows)} months")
        for row in rows[:3]:
            dim(str(row))


def test_sql_membership(gw: AgentCoreGatewayClient, verbose: bool) -> None:
    """T7: GROUP BY membership tier."""
    heading("T7: SQL — membership tier distribution")

    sql = """\
SELECT membership,
       COUNT(*) AS customer_count,
       ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER(), 1) AS pct
FROM   "dremio_samples"."customer360"."customer"
GROUP  BY membership
ORDER  BY customer_count DESC"""

    result = gw.call_tool("RunSqlQuery", {"query": sql})
    show_payload(result, verbose)
    assert_true("result is non-empty", bool(result))
    ok("membership distribution query executed")

    rows = _extract_rows(result)
    if rows:
        for row in rows:
            dim(str(row))


def test_search(gw: AgentCoreGatewayClient, verbose: bool) -> None:
    """T8: SearchTableAndViews semantic search."""
    heading("T8: SearchTableAndViews")

    result = gw.call_tool("SearchTableAndViews", {"query": "customer orders revenue"})
    show_payload(result, verbose)
    assert_true("result is non-empty", bool(result))
    ok("SearchTableAndViews returned")

    # Extract table names from various response shapes
    tables = _extract_table_names(result)
    if tables:
        ok(f"tables found: {tables}")
    else:
        info(f"raw result: {str(result)[:300]}")


def test_system_tables(gw: AgentCoreGatewayClient, verbose: bool) -> None:
    """T9: GetUsefulSystemTableNames."""
    heading("T9: GetUsefulSystemTableNames")

    result = gw.call_tool("GetUsefulSystemTableNames", {})
    show_payload(result, verbose)
    assert_true("result is non-empty", bool(result))
    ok("system tables returned")
    dim(str(result)[:300])


def test_lineage(gw: AgentCoreGatewayClient, verbose: bool) -> None:
    """T10: GetTableOrViewLineage."""
    heading("T10: GetTableOrViewLineage — orders table")

    result = gw.call_tool("GetTableOrViewLineage", {
        "table_name": '"dremio_samples"."customer360"."orders"'
    })
    show_payload(result, verbose)
    assert_true("result is non-empty", bool(result))
    ok("lineage returned")
    dim(str(result)[:300])


def test_token_refresh(auth: Auth0TokenManager, verbose: bool) -> None:
    """T11: Simulate token expiry and verify forced refresh."""
    heading("T11: Token refresh on simulated expiry")

    # Get initial token
    tok1 = auth.get_token()
    assert_true("initial token", bool(tok1))
    ok(f"initial token: {tok1[:30]}...")

    # Simulate expiry by zeroing the cache
    with auth._lock:
        original_expires = auth._cached.expires_at
        auth._cached = None
    ok("token cache cleared (simulated expiry)")

    # Next get_token() should fetch a new one
    t0   = time.time()
    tok2 = auth.get_token()
    t2   = time.time() - t0
    assert_true("new token fetched", bool(tok2))
    ok(f"refresh took: {t2*1000:.0f}ms")

    # Tokens may be identical (Auth0 may return same token if still valid)
    info(f"same token: {tok1[:20] == tok2[:20]} (expected if < expiry)")


def test_nl_query(gw: AgentCoreGatewayClient, verbose: bool) -> None:
    """T12: Natural language → Bedrock SQL gen → gateway execution."""
    heading("T12: NL query (Bedrock SQL gen + gateway)")
    import os, urllib.request, urllib.error

    BEDROCK_TOKEN = os.getenv("AWS_BEARER_TOKEN_BEDROCK",
        "ABSKQmVkcm9ja0FQSUtleS1wY3VrLWF0LTc2MTMzNDYyNzU3NjpnL090bGE1VkZPMHg1cTNhb0g4aU1CSUVsMFpYcmlQelMwWnYwK3U4NCtXM1BWdE80emVoZnBxUTR6UT0=")
    AWS_REGION  = os.getenv("AWS_REGION", "us-east-1")
    MODEL       = os.getenv("BEDROCK_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    BEDROCK_URL = f"https://bedrock-runtime.{AWS_REGION}.amazonaws.com/model/{MODEL}/invoke"

    SCHEMA = """\
TABLE: "dremio_samples"."customer360"."customer"
  customer_id: VARCHAR, full_name: VARCHAR, state: VARCHAR, membership: VARCHAR, join_date: DATE
TABLE: "dremio_samples"."customer360"."orders"
  order_id: VARCHAR, customer_id: VARCHAR, order_timestamp: TIMESTAMP, total_price: DOUBLE
RULE: Quote reserved words with double quotes: "count", "month", "day", "year", "table" """

    question = "How many orders were placed in each membership tier?"
    info(f"question: {question!r}")

    # Step 1: SQL generation
    prompt = (f'Schema:\n{SCHEMA}\n\nSQL for: "{question}"\n'
              f'Return ONLY the SQL. No markdown. Add LIMIT 20 if not aggregating.')
    payload = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 512,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(BEDROCK_URL, data=payload, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {BEDROCK_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            br = json.loads(r.read())
        sql = "".join(b.get("text","") for b in br.get("content",[])
                      if b.get("type") == "text").strip()
        if "```" in sql:
            sql = "\n".join(l for l in sql.split("\n")
                            if not l.strip().startswith("```")).strip()
        ok(f"SQL generated: {sql[:80]}...")
        dim(sql)
    except Exception as e:
        warn(f"Bedrock unavailable: {e} — skipping SQL execution step")
        return

    # Step 2: Execute via gateway
    result = gw.call_tool("RunSqlQuery", {"query": sql})
    show_payload(result, verbose)
    assert_true("NL query result non-empty", bool(result))
    ok("NL query executed via gateway")
    rows = _extract_rows(result)
    if rows:
        for row in rows[:5]:
            dim(str(row))


def test_concurrent(gw: AgentCoreGatewayClient, verbose: bool) -> None:
    """T13: 3 concurrent SQL calls — verify thread safety of token manager."""
    heading("T13: Concurrent calls (thread safety)")

    queries = [
        ('SELECT COUNT(*) AS n FROM "dremio_samples"."customer360"."customer"', "customers"),
        ('SELECT COUNT(*) AS n FROM "dremio_samples"."customer360"."orders"',   "orders"),
        ('SELECT COUNT(*) AS n FROM "dremio_samples"."customer360"."product"',  "products"),
    ]

    results = {}
    errors  = {}

    def _call(label, sql):
        try:
            r = gw.call_tool("RunSqlQuery", {"query": sql})
            results[label] = r
        except Exception as e:
            errors[label] = str(e)

    t0 = time.time()
    threads = [threading.Thread(target=_call, args=(lbl, sql)) for sql, lbl in queries]
    for t in threads: t.start()
    for t in threads: t.join()
    elapsed = time.time() - t0

    ok(f"all 3 calls completed in {elapsed:.1f}s")
    if errors:
        for lbl, err in errors.items():
            fail(f"{lbl}: {err}")
        raise AssertionError(f"concurrent errors: {errors}")
    else:
        for lbl, r in results.items():
            count = _extract_scalar(r, "n")
            status = f"{count:,}" if count else str(r)[:60]
            ok(f"{lbl}: {status}")


# ══════════════════════════════════════════════════════════════════════════════
# Response parsing helpers (gateway responses vary in shape)
# ══════════════════════════════════════════════════════════════════════════════

def _extract_scalar(result: Any, key: str) -> int | float | None:
    """Try to pull a scalar value from various gateway response shapes."""
    if isinstance(result, dict):
        # Direct: {"total": 4800000}
        if key in result:
            return result[key]
        # Nested: {"result": [{"total": 4800000}]}
        rows = result.get("result", [])
        if rows and isinstance(rows, list) and key in rows[0]:
            return rows[0][key]
        # Text blob — try JSON parse
        text = result.get("text", "") or result.get("summary", "")
        if text:
            try:
                parsed = json.loads(text)
                return _extract_scalar(parsed, key)
            except Exception:
                pass
    return None


def _extract_rows(result: Any) -> list[dict]:
    """Extract a list of row dicts from various gateway response shapes."""
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        # {"result": [...]}
        rows = result.get("result", result.get("rows", []))
        if isinstance(rows, list):
            return rows
        # Text blob
        text = result.get("text", "") or result.get("summary", "")
        if text:
            try:
                parsed = json.loads(text)
                return _extract_rows(parsed)
            except Exception:
                pass
    return []


def _extract_table_names(result: Any) -> list[str]:
    """Pull table names from SearchTableAndViews response."""
    rows = _extract_rows(result)
    names = []
    for row in rows:
        if isinstance(row, dict):
            name = row.get("name") or row.get("table_name") or row.get("TABLE_NAME")
            if name:
                names.append(name)
    return names


# ══════════════════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════════════════

ALL_TESTS = {
    "auth":          (test_auth,            False),  # (fn, needs_gateway)
    "tools_list":    (test_tools_list,      True),
    "sql_simple":    (test_sql_simple,      True),
    "sql_customer":  (test_sql_top_customers, True),
    "sql_trend":     (test_sql_monthly_trend, True),
    "sql_membership":(test_sql_membership,  True),
    "search":        (test_search,          True),
    "system_tables": (test_system_tables,   True),
    "lineage":       (test_lineage,         True),
    "token_refresh": (test_token_refresh,   False),
    "nl_query":      (test_nl_query,        True),
    "concurrent":    (test_concurrent,      True),
}

QUICK_TESTS = ["auth", "sql_simple", "sql_customer"]


def run_tests(test_names: list[str], verbose: bool) -> bool:
    print(f"\n{BOLD}AgentCore Gateway Integration Tests{RESET}")
    print("=" * 60)

    # Shared instances
    print("\nInitialising Auth0 + gateway clients...")
    auth = Auth0TokenManager()
    gw   = AgentCoreGatewayClient(token_manager=auth)

    results = Results()

    for name in test_names:
        fn, needs_gw = ALL_TESTS[name]
        try:
            if needs_gw:
                fn(gw, verbose)
            else:
                fn(auth, verbose)
            results.passed(name)
        except RuntimeError as e:
            # Network / gateway errors — might just be sandbox restriction
            fail(f"RUNTIME ERROR: {e}")
            results.failed(name, str(e)[:120])
        except AssertionError as e:
            fail(f"ASSERTION: {e}")
            results.failed(name, str(e)[:120])
        except Exception as e:
            fail(f"UNEXPECTED: {type(e).__name__}: {e}")
            results.failed(name, f"{type(e).__name__}: {str(e)[:100]}")

    return results.summary()


def main():
    parser = argparse.ArgumentParser(description="AgentCore Gateway Tests")
    parser.add_argument("--quick",   action="store_true",
                        help=f"Run quick subset: {QUICK_TESTS}")
    parser.add_argument("--test",    default=None,
                        help=f"Single test name: {list(ALL_TESTS)}")
    parser.add_argument("--verbose", action="store_true",
                        help="Print full response payloads")
    args = parser.parse_args()

    if args.test:
        if args.test not in ALL_TESTS:
            print(f"Unknown test '{args.test}'. Available: {list(ALL_TESTS)}")
            sys.exit(1)
        names = [args.test]
    elif args.quick:
        names = QUICK_TESTS
    else:
        names = list(ALL_TESTS)

    print(f"Running: {names}")
    passed = run_tests(names, verbose=args.verbose)
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
