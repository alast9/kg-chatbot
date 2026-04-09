"""capabilities/knowledge_graph.py — Neo4j graph + ES descriptions + DuckDB cost analytics."""
from __future__ import annotations
import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Any

from .base import Capability

log = logging.getLogger("cap.kg")

NEO4J_URI      = os.getenv("NEO4J_URI",      "neo4j+s://afc6eb9c.databases.neo4j.io")
NEO4J_USER     = os.getenv("NEO4J_USER",     "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "DmDoQPVveXf_8I8fy08mlmAlZ5I1fZCxSC-FByIIexM")
ES_URL  = os.getenv("ES_URL",  "https://my-elasticsearch-project-b00ed2.es.eastus.azure.elastic.cloud:443")
ES_KEY  = os.getenv("ES_KEY",  "b3F0QlhwMEJ3d040MXBZVml1N0k6anZWZndISkx2eTRUQVg1WUlkTXcxQQ==")
ES_IDX  = os.getenv("ES_IDX",  "kg_descriptions")
MCP_BASE = os.getenv("MCP_BASE", "http://localhost:443")
KB_DIR   = Path(os.getenv("KB_DIR", str(Path.home() / "data/rawdata/kb")))

_DUCKDB_SCHEMA_CONTEXT = """\
DUCKDB COST ANALYTICS — data range 2026-01-01 to 2026-03-31.

PREFERRED VIEWS for cost queries (use these — they handle attribution automatically):
  app_compute_cost(app_id INT, datetime TIMESTAMP, cost FLOAT, platform TEXT)
    — dremio + snowflake query costs, already split equally across each user's apps
  app_storage_cost(app_id INT, datetime TIMESTAMP, cost FLOAT, s3_bucket TEXT, s3_folder TEXT)
    — S3 storage costs per application per day

REFERENCE TABLES (exact column names — do not guess or abbreviate):
  lob(lob_id INT, lob_name TEXT)
  cost_center(cost_center_id INT, cost_center_name TEXT, lob_id INT)
  application(app_id INT, app_name TEXT, cost_center_id INT)
  users(user_id TEXT, user_name TEXT)
  user_app_access(user_id TEXT, app_id INT)

RAW USAGE TABLES (only needed for platform-specific or warehouse-level queries):
  dremio_usage(datetime TIMESTAMP, user_id TEXT, query_id TEXT, query_time FLOAT, query_cost FLOAT)
  snowflake_usage(datetime TIMESTAMP, user_id TEXT, query_id TEXT, query_time FLOAT, query_cost FLOAT, warehouse_name TEXT)
    — cost/usage metrics only; NOT the same as Snowflake DEMO_DB business data (use snowflake_run_sql for that)
  s3_usage(datetime TIMESTAMP, application_id INT, s3_bucket TEXT, s3_folder TEXT, storage_cost FLOAT)

NEVER use tables named: cost_data, cost_analytics, costs, compute_costs — they do not exist.

Example — top 3 cost centers in March 2026 (compute + storage combined):
  SELECT cc.cost_center_name,
         ROUND(SUM(c.cost), 2) AS total_cost
  FROM (
      SELECT app_id, cost FROM app_compute_cost
      WHERE datetime >= '2026-03-01' AND datetime < '2026-04-01'
      UNION ALL
      SELECT app_id, cost FROM app_storage_cost
      WHERE datetime >= '2026-03-01' AND datetime < '2026-04-01'
  ) c
  JOIN application a ON c.app_id = a.app_id
  JOIN cost_center cc ON a.cost_center_id = cc.cost_center_id
  GROUP BY cc.cost_center_name
  ORDER BY total_cost DESC
  LIMIT 3;\
"""


class KnowledgeGraphCapability(Capability):
    name = "knowledge_graph"
    description = "Application portfolio graph, cost analytics (Dremio/Snowflake/S3), and app descriptions"

    def __init__(self):
        self._neo4j_driver = None
        self._es_client    = None
        self._mcp_ok       = False
        self._init_neo4j()
        self._init_es()
        self._init_mcp()

    # ── Connectivity ──────────────────────────────────────────────────────────

    def _init_neo4j(self):
        try:
            from neo4j import GraphDatabase
            drv = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
            with drv.session(database="neo4j") as s: s.run("RETURN 1")
            self._neo4j_driver = drv
            log.info("Neo4j connected")
        except Exception as e:
            log.warning("Neo4j unavailable: %s", e)

    def _init_es(self):
        try:
            from elasticsearch import Elasticsearch
            es = Elasticsearch(ES_URL, api_key=ES_KEY, request_timeout=15)
            es.info()
            self._es_client = es
            log.info("Elasticsearch connected")
        except Exception as e:
            log.warning("ES unavailable: %s", e)

    def _init_mcp(self):
        try:
            urllib.request.urlopen(f"{MCP_BASE}/health", timeout=5)
            self._mcp_ok = True
            log.info("DuckDB MCP server connected")
        except Exception as e:
            log.warning("DuckDB MCP unavailable: %s", e)

    def startup_check(self):
        parts = [
            f"Neo4j {'✓' if self._neo4j_driver else '✗'}",
            f"ES {'✓' if self._es_client else '✗'}",
            f"DuckDB MCP {'✓' if self._mcp_ok else '✗ (start mcp_cost_server_v2.py)'}",
        ]
        ok = self._neo4j_driver is not None
        return ok, " | ".join(parts)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _q(self, cypher: str, **params) -> list[dict]:
        if not self._neo4j_driver:
            return [{"error": "Neo4j unavailable"}]
        with self._neo4j_driver.session(database="neo4j") as s:
            return [dict(r) for r in s.run(cypher, **params)]

    def _mcp_sql(self, sql: str, desc: str = "") -> dict:
        if not self._mcp_ok:
            return {"error": "DuckDB MCP unavailable — run mcp_cost_server_v2.py"}
        import time
        body = json.dumps({"sql": sql, "description": desc}).encode()
        req  = urllib.request.Request(
            f"{MCP_BASE}/query/sql", data=body, method="POST",
            headers={"Content-Type": "application/json"})
        # 2LO: attach Entra ID client_credentials token so the MCP server
        # can verify the caller is the authorised chatbot application.
        try:
            from auth.sso import get_mcp_token_manager
            tok = get_mcp_token_manager().get_token()
            req.add_header("Authorization", f"Bearer {tok}")
        except Exception as e:
            log.warning("MCP auth token unavailable (%s) — proceeding unauthenticated", e)
        log.info("MCP request [%s]: %.200s", desc or "sql", sql)
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                result = json.loads(r.read())
            elapsed = time.monotonic() - t0
            log.info("MCP response: %d rows in %.2fs", result.get("row_count", "?"), elapsed)
            return result
        except Exception as e:
            elapsed = time.monotonic() - t0
            log.error("MCP error after %.2fs: %s", elapsed, e)
            return {"error": str(e)}

    # ── Static context (cached in system prompt) ──────────────────────────────

    def static_context(self) -> str:
        lines = []
        try:
            lobs = json.load(open(KB_DIR / "lobs.json"))
            lines.append("KNOWLEDGE GRAPH — LOB → COST CENTER STRUCTURE (Neo4j)")
            for lob in lobs:
                lines.append(f"  LOB: {lob['lob_name']}  [id={lob['lob_id']}]")
                for cc in lob["cost_centers"]:
                    lines.append(f"    CC: {cc['cost_center_name']}  [id={cc['cost_center_id']}]")
            lines.append("")
        except FileNotFoundError:
            pass
        lines.append(_DUCKDB_SCHEMA_CONTEXT)
        return "\n".join(lines)

    def system_fragment(self) -> str:
        return """\
KNOWLEDGE GRAPH & COST ANALYTICS
  Graph questions (who accesses what, LOB/CC structure) → kg_* Neo4j tools.
  Description questions (what does X do?) → kg_describe_entity or kg_search_knowledge.
  Cost/spend/trend questions → execute_sql with DuckDB SQL.
  Always use the exact table and column names from the schema — never guess or invent names.
  Attribution: split compute cost equally across user_app_access apps per user.
  IMPORTANT: snowflake_usage in DuckDB contains cost/usage metrics only.
    For Snowflake DEMO_DB business data → use snowflake_run_sql (Snowflake capability)."""

    # ── Tools ─────────────────────────────────────────────────────────────────

    def tools(self) -> list[dict]:
        return [
            {"name": "kg_get_stats",
             "description": "KB graph counts: users, applications, cost centers, LOBs.",
             "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "kg_get_all_lobs",
             "description": "List all Lines of Business with cost center counts.",
             "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "kg_get_cost_centers_for_lob",
             "description": "Cost centers in a Line of Business.",
             "input_schema": {"type": "object",
                              "properties": {"lob_name": {"type": "string"}},
                              "required": ["lob_name"]}},
            {"name": "kg_get_apps_for_lob",
             "description": "All applications in a Line of Business.",
             "input_schema": {"type": "object",
                              "properties": {"lob_name": {"type": "string"}},
                              "required": ["lob_name"]}},
            {"name": "kg_get_user_apps",
             "description": "Applications a user has access to.",
             "input_schema": {"type": "object",
                              "properties": {"user_name": {"type": "string"}},
                              "required": ["user_name"]}},
            {"name": "kg_search_application",
             "description": "Find an application by keyword — returns app_id for SQL queries.",
             "input_schema": {"type": "object",
                              "properties": {"term": {"type": "string"}},
                              "required": ["term"]}},
            {"name": "kg_run_cypher",
             "description": "Raw Cypher against Neo4j for complex graph traversals.",
             "input_schema": {"type": "object",
                              "properties": {"cypher": {"type": "string"}},
                              "required": ["cypher"]}},
            {"name": "kg_describe_entity",
             "description": "Describe an application or cost center ('what does X do?').",
             "input_schema": {"type": "object",
                              "properties": {
                                  "name": {"type": "string"},
                                  "entity_type": {"type": "string",
                                                  "enum": ["application", "cost_center"]}},
                              "required": ["name"]}},
            {"name": "kg_search_knowledge",
             "description": "Semantic search over app/CC descriptions ('apps supporting risk', 'tools for audit').",
             "input_schema": {"type": "object",
                              "properties": {
                                  "query": {"type": "string"},
                                  "entity_type": {"type": "string",
                                                  "enum": ["application", "cost_center"]},
                                  "top_k": {"type": "integer"}},
                              "required": ["query"]}},
            {"name": "execute_sql",
             "description": (
                 "Run a read-only SQL SELECT against DuckDB cost analytics. "
                 "Use for all cost, spend, trend, and usage questions. "
                 "Always apply attribution rule (user_app_count CTE) for app/CC/LOB roll-ups."
             ),
             "input_schema": {"type": "object",
                              "properties": {
                                  "sql":         {"type": "string"},
                                  "description": {"type": "string"}},
                              "required": ["sql"]}},
        ]

    # ── Tool execution ────────────────────────────────────────────────────────

    def handle_tool(self, name: str, inputs: dict) -> Any:
        if name == "kg_get_stats":
            c = {r["label"]: r["cnt"] for r in self._q(
                "MATCH (n) RETURN labels(n)[0] AS label, count(n) AS cnt")}
            acc = self._q("MATCH ()-[r:HAS_ACCESS]->() RETURN count(r) AS cnt")
            return {**c, "access_grants": acc[0].get("cnt", 0) if acc else 0}
        if name == "kg_get_all_lobs":
            return self._q("MATCH (l:LineOfBusiness) OPTIONAL MATCH (c:CostCenter)-[:BELONGS_TO]->(l) RETURN l.lob_id AS lob_id, l.name AS name, count(c) AS cc_count ORDER BY l.name")
        if name == "kg_get_cost_centers_for_lob":
            return self._q("MATCH (c:CostCenter)-[:BELONGS_TO]->(l:LineOfBusiness) WHERE toLower(l.name) CONTAINS toLower($n) RETURN c.cost_center_id AS id, c.name AS name, l.name AS lob ORDER BY c.name", n=inputs["lob_name"])
        if name == "kg_get_apps_for_lob":
            return self._q("MATCH (a:Application)-[:UNDER]->(c:CostCenter)-[:BELONGS_TO]->(l:LineOfBusiness) WHERE toLower(l.name) CONTAINS toLower($n) RETURN a.app_id AS id, a.name AS name, c.name AS cost_center ORDER BY c.name, a.name", n=inputs["lob_name"])
        if name == "kg_get_user_apps":
            return self._q("MATCH (u:User)-[:HAS_ACCESS]->(a:Application)-[:UNDER]->(c:CostCenter)-[:BELONGS_TO]->(l:LineOfBusiness) WHERE toLower(u.name) CONTAINS toLower($n) RETURN u.name AS user, a.name AS app, c.name AS cost_center, l.name AS lob ORDER BY l.name, a.name", n=inputs["user_name"])
        if name == "kg_search_application":
            return self._q("MATCH (a:Application)-[:UNDER]->(c:CostCenter)-[:BELONGS_TO]->(l:LineOfBusiness) WHERE toLower(a.name) CONTAINS toLower($t) OPTIONAL MATCH (u:User)-[:HAS_ACCESS]->(a) RETURN a.app_id AS id, a.name AS name, c.name AS cost_center, l.name AS lob, count(u) AS users ORDER BY users DESC", t=inputs["term"])
        if name == "kg_run_cypher":
            return self._q(inputs["cypher"])
        if name == "kg_describe_entity":
            if not self._es_client:
                return {"error": "Elasticsearch unavailable"}
            q = {"query": {"bool": {"must": [{"multi_match": {"query": inputs["name"],
                "fields": ["entity_name^4", "description", "full_context"]}}],
                "filter": [{"term": {"entity_type": inputs["entity_type"]}}]
                           if inputs.get("entity_type") else []}},
                "sort": [{"desc_index": "asc"}], "size": 9,
                "_source": {"excludes": ["embedding"]}}
            hits = self._es_client.search(index=ES_IDX, body=q)["hits"]["hits"]
            if not hits: return {"found": False}
            entities: dict = {}
            for h in hits:
                s = h["_source"]; eid = s["entity_id"]
                if eid not in entities:
                    entities[eid] = {"entity_name": s["entity_name"],
                                     "entity_type": s["entity_type"],
                                     "lob": s.get("lob_name"), "descriptions": []}
                entities[eid]["descriptions"].append(s["description"])
            return {"found": True, "results": list(entities.values())}
        if name == "kg_search_knowledge":
            if not self._es_client:
                return {"error": "Elasticsearch unavailable"}
            fc = [{"term": {"entity_type": inputs["entity_type"]}}] if inputs.get("entity_type") else []
            body = {"query": {"bool": {"must": [{"multi_match": {"query": inputs["query"],
                    "fields": ["description", "full_context", "entity_name^2"]}}],
                    "filter": fc}},
                    "size": inputs.get("top_k", 5), "_source": {"excludes": ["embedding"]}}
            hits = self._es_client.search(index=ES_IDX, body=body)["hits"]["hits"]
            return {"results": [{"entity_name": h["_source"]["entity_name"],
                                 "entity_type": h["_source"]["entity_type"],
                                 "lob": h["_source"].get("lob_name"),
                                 "text": h["_source"]["description"]} for h in hits]}
        if name == "execute_sql":
            return self._mcp_sql(inputs["sql"], inputs.get("description", ""))
        return None
