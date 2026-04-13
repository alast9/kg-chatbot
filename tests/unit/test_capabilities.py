"""
tests/unit/test_capabilities.py — Unit tests for capability modules.

Covers:
  - DremioCapability  (capabilities/dremio.py)
  - KnowledgeGraphCapability (capabilities/knowledge_graph.py)
"""
from __future__ import annotations

import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# TestDremioCapability
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestDremioCapability:

    def test_startup_check_passes_when_pat_set(self, monkeypatch):
        """startup_check() passes when DREMIO_PAT is set."""
        monkeypatch.setenv("DREMIO_PAT", "fake-pat-token")
        from capabilities.dremio import DremioCapability
        cap = DremioCapability()
        ok, msg = cap.startup_check()
        assert ok is True
        assert "ready" in msg.lower() or "dremio" in msg.lower()

    def test_startup_check_fails_without_credentials(self, monkeypatch):
        """startup_check() fails when neither PAT nor OAuth client ID is set."""
        monkeypatch.delenv("DREMIO_PAT", raising=False)
        monkeypatch.delenv("DREMIO_OAUTH_CLIENT_ID", raising=False)
        from capabilities.dremio import DremioCapability
        cap = DremioCapability()
        ok, msg = cap.startup_check()
        assert ok is False

    def test_tools_list_has_required_tools(self, dremio_cap):
        """dremio_run_sql must be registered."""
        names = [t["name"] for t in dremio_cap.tools()]
        assert "dremio_run_sql" in names

    def test_handles_known_tools(self, dremio_cap):
        """handles() returns True for registered tool names."""
        assert dremio_cap.handles("dremio_run_sql") is True

    def test_returns_none_for_unknown_tool(self, dremio_cap):
        """handle_tool returns None for a tool not owned by this capability."""
        result = dremio_cap.handle_tool("kg_get_stats", {})
        assert result is None

    def test_dremio_run_sql_calls_exec_sql(self, dremio_cap):
        """dremio_run_sql invokes _exec_sql with the user's token."""
        dremio_cap._dremio_token = "fake-token"
        dremio_cap._dremio_token_expires_at = time.time() + 3600

        fake_result = {"rows": [["Alice", 100]], "columns": ["name", "spend"],
                       "row_count": 1, "job_id": "j1"}
        with patch("capabilities.dremio._exec_sql", return_value=fake_result):
            result = dremio_cap.handle_tool("dremio_run_sql", {"sql": "SELECT 1"})

        assert result is not None
        assert "rows" in result

    def test_set_dremio_token_stores_token(self, dremio_cap):
        """_dremio_token updated after set_dremio_token()."""
        dremio_cap.set_dremio_token("my-token", time.time() + 3600)
        with dremio_cap._lock:
            assert dremio_cap._dremio_token == "my-token"

    def test_set_dremio_token_thread_safe(self, dremio_cap):
        """20 concurrent set_dremio_token() calls produce no race condition."""
        errors = []

        def worker(i):
            try:
                dremio_cap.set_dremio_token(f"token-{i}", time.time() + 3600)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []

    def test_fallback_to_pat_when_no_user_token(self, monkeypatch):
        """When user token absent and DREMIO_PAT set, PAT is used as fallback."""
        monkeypatch.setenv("DREMIO_PAT", "service-pat")
        from capabilities.dremio import DremioCapability
        cap = DremioCapability()
        token = cap._token()
        assert token == "service-pat"

    def test_raises_when_no_token_available(self, monkeypatch):
        """RuntimeError raised when no user token and no PAT."""
        monkeypatch.delenv("DREMIO_PAT", raising=False)
        from capabilities.dremio import DremioCapability
        cap = DremioCapability()
        cap._dremio_token = ""
        with pytest.raises(RuntimeError):
            cap._token()

    def test_static_context_contains_schema(self, dremio_cap):
        """Schema catalog contains customer360, customer_id, total_price."""
        ctx = dremio_cap.static_context()
        assert "customer360" in ctx
        assert "customer_id" in ctx
        assert "total_price" in ctx

    def test_system_fragment_mentions_dremio(self, dremio_cap):
        """System prompt fragment contains 'dremio' and 'dremio_run_sql'."""
        frag = dremio_cap.system_fragment()
        assert "dremio" in frag.lower()
        assert "dremio_run_sql" in frag


# ─────────────────────────────────────────────────────────────────────────────
# TestKnowledgeGraphCapability
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestKnowledgeGraphCapability:

    def test_startup_check_neo4j_connected(self, kg_cap):
        """Startup check returns (True, msg with 'Neo4j') when driver is set."""
        ok, msg = kg_cap.startup_check()
        assert ok is True
        assert "Neo4j" in msg

    def test_tools_list_has_required_tools(self, kg_cap):
        """All required KG tools are registered."""
        names = [t["name"] for t in kg_cap.tools()]
        for required in ["kg_get_stats", "kg_get_all_lobs", "kg_describe_entity",
                         "kg_search_knowledge", "execute_sql"]:
            assert required in names, f"Missing tool: {required}"

    def test_kg_get_stats_calls_neo4j(self, kg_cap, mock_neo4j):
        """kg_get_stats triggers driver.session() call."""
        mock_neo4j.session.return_value.__enter__.return_value.run.return_value = []
        kg_cap.handle_tool("kg_get_stats", {})
        mock_neo4j.session.assert_called()

    def test_kg_describe_entity_calls_es(self, kg_cap, mock_es):
        """kg_describe_entity triggers es.search() exactly once."""
        mock_es.search.return_value = {"hits": {"hits": []}}
        kg_cap.handle_tool("kg_describe_entity", {
            "name": "Equity Risk Engine",
            "entity_type": "application",
        })
        mock_es.search.assert_called_once()

    def test_kg_describe_entity_returns_not_found(self, kg_cap, mock_es):
        """Empty ES hits → {'found': False}."""
        mock_es.search.return_value = {"hits": {"hits": []}}
        result = kg_cap.handle_tool("kg_describe_entity", {
            "name": "Unknown App",
            "entity_type": "application",
        })
        assert result == {"found": False}

    def test_execute_sql_returns_error_when_mcp_unavailable(self, kg_cap):
        """With mcp_ok=False, execute_sql returns {'error': ...}."""
        kg_cap._mcp_ok = False
        result = kg_cap.handle_tool("execute_sql", {"sql": "SELECT 1"})
        assert isinstance(result, dict)
        assert "error" in result

    def test_unknown_tool_returns_none(self, kg_cap):
        """handle_tool('dremio_run_sql', {}) returns None."""
        result = kg_cap.handle_tool("dremio_run_sql", {})
        assert result is None

    def test_static_context_includes_duckdb_schema(self, kg_cap):
        """static_context() includes the DuckDB schema block."""
        ctx = kg_cap.static_context()
        assert "app_compute_cost" in ctx
        assert "cost_center" in ctx
        assert "execute_sql" in ctx or "DuckDB" in ctx

    def test_handles_all_registered_tool_names(self, kg_cap):
        """handles() returns True for every tool in tools()."""
        for t in kg_cap.tools():
            assert kg_cap.handles(t["name"]) is True

    def test_system_fragment_mentions_knowledge_graph(self, kg_cap):
        """system_fragment() describes the KG capability."""
        frag = kg_cap.system_fragment()
        assert "knowledge" in frag.lower() or "graph" in frag.lower() or "cost" in frag.lower()
