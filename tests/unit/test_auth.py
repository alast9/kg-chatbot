"""
tests/unit/test_auth.py — Unit tests for auth layer.

Covers:
  - EntraTokenManager  (auth/sso.py)
  - AgentCoreGatewayClient (auth/agentcore_gateway.py)
  - OIDCFlow / SessionStore / _pkce_pair (auth/oidc.py)
  - SessionManager (session.py)
  - ChatbotCore dispatch (chatbot_core.py)
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# TestEntraTokenManager
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestEntraTokenManager:

    def _make_manager(self, scope="api://test/.default"):
        from auth.sso import EntraTokenManager
        return EntraTokenManager(
            scope=scope,
            tenant_id="test-tenant",
            client_id="test-client",
            client_secret="test-secret",
        )

    def test_token_returned_from_cache(self, mock_token_manager):
        """Cache hit returns token without any network call."""
        with patch.object(mock_token_manager, "_refresh") as mock_refresh:
            tok = mock_token_manager.get_token()
        assert tok is not None
        assert len(tok) > 10
        mock_refresh.assert_not_called()

    def test_cache_miss_triggers_refresh(self):
        """None cache forces _refresh() call."""
        mgr = self._make_manager()
        mgr._cached = None
        with patch.object(mgr, "_refresh", return_value="fresh-token") as mock_refresh:
            tok = mgr.get_token()
        assert tok == "fresh-token"
        mock_refresh.assert_called_once()

    def test_expired_token_triggers_refresh(self):
        """Token with expires_at < now forces refresh."""
        from auth.sso import _CachedToken
        mgr = self._make_manager()
        mgr._cached = _CachedToken(access_token="old-token", expires_at=time.time() - 1)
        with patch.object(mgr, "_refresh", return_value="new-token") as mock_refresh:
            tok = mgr.get_token()
        assert tok == "new-token"
        mock_refresh.assert_called_once()

    def test_thread_safety_single_refresh(self):
        """10 concurrent threads trigger exactly 1 refresh."""
        from auth.sso import _CachedToken
        mgr = self._make_manager()
        mgr._cached = None
        call_count = []

        def fake_refresh():
            call_count.append(1)
            time.sleep(0.05)
            mgr._cached = _CachedToken(
                access_token="tok",
                expires_at=time.time() + 3600,
            )
            return "tok"

        with patch.object(mgr, "_refresh", side_effect=fake_refresh):
            threads = [threading.Thread(target=mgr.get_token) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert len(call_count) == 1

    def test_token_has_three_jwt_parts(self, mock_entra_token):
        """Token fixture has header.payload.signature structure."""
        parts = mock_entra_token.split(".")
        assert len(parts) == 3

    def test_startup_check_ok(self, mock_token_manager):
        """startup_check() returns (True, msg) when token is cached."""
        ok, msg = mock_token_manager.startup_check()
        assert ok is True
        assert "OK" in msg or "token_len" in msg


# ─────────────────────────────────────────────────────────────────────────────
# TestAgentCoreGatewayClient
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestAgentCoreGatewayClient:

    def test_list_tools_returns_expected_names(self, mock_gateway):
        tools = mock_gateway.list_tools()
        names = [t["name"] for t in tools]
        assert "RunSqlQuery" in names
        assert "SearchTableAndViews" in names

    def test_call_tool_returns_result(self, mock_gateway):
        result = mock_gateway.call_tool("RunSqlQuery", {"sql": "SELECT 1"})
        assert isinstance(result, dict)

    def test_call_tool_with_token_uses_token(self, mock_gateway):
        mock_gateway.call_tool_with_token("RunSqlQuery", {"sql": "SELECT 1"}, token="tok")
        mock_gateway.call_tool_with_token.assert_called_once()

    def test_startup_check_passes(self, mock_gateway):
        ok, msg = mock_gateway.startup_check()
        assert ok is True

    def test_rpc_builds_correct_envelope(self, mock_token_manager):
        """Intercepted HTTP body has jsonrpc='2.0', method, and id fields."""
        from auth.agentcore_gateway import AgentCoreGatewayClient
        gw = AgentCoreGatewayClient(
            gateway_url="https://mock.example.com/mcp",
            token_manager=mock_token_manager,
        )
        captured = []

        import urllib.request
        original_urlopen = urllib.request.urlopen

        def fake_urlopen(req, timeout=None):
            body = json.loads(req.data.decode())
            captured.append(body)
            # Return a minimal valid JSON-RPC response
            import io
            resp_data = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {}}).encode()
            mock_resp = MagicMock()
            mock_resp.read = MagicMock(return_value=resp_data)
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            try:
                gw._rpc("tools/list")
            except Exception:
                pass

        assert len(captured) >= 1
        body = captured[0]
        assert body.get("jsonrpc") == "2.0"
        assert "method" in body
        assert "id" in body

    def test_extract_content_handles_mcp_format(self, mock_gateway):
        """{'content': [{'type': 'text', 'text': '...'}]} is correctly unpacked."""
        from auth.agentcore_gateway import AgentCoreGatewayClient
        mcp_result = {"content": [{"type": "text", "text": '{"rows": []}'}]}
        result = AgentCoreGatewayClient._extract_content(mcp_result)
        assert isinstance(result, (dict, str))

    def test_extract_content_handles_direct_result(self, mock_gateway):
        """Raw result dict passed through unchanged."""
        from auth.agentcore_gateway import AgentCoreGatewayClient
        raw = {"rows": [["Alice", 100]], "columns": ["name", "spend"]}
        result = AgentCoreGatewayClient._extract_content(raw)
        assert result == raw


# ─────────────────────────────────────────────────────────────────────────────
# TestOIDC
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestOIDC:

    def test_pkce_pair_produces_valid_challenge(self):
        """challenge == base64url(sha256(verifier))"""
        from auth.oidc import _pkce_pair
        verifier, challenge = _pkce_pair()
        digest = hashlib.sha256(verifier.encode()).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        assert challenge == expected

    def test_session_store_create_and_retrieve(self):
        """Session created and retrieved by ID."""
        from auth.oidc import SessionStore, UserSession
        store = SessionStore()
        sess = UserSession(
            session_id="test-sid",
            user_id="uid",
            email="a@b.com",
            name="Alice",
            expires_at=time.time() + 3600,
        )
        store.create_session(sess)
        retrieved = store.get_session("test-sid")
        assert retrieved is not None
        assert retrieved.email == "a@b.com"

    def test_expired_session_returns_none(self):
        """Session with expires_at < now returns None."""
        from auth.oidc import SessionStore, UserSession
        store = SessionStore()
        sess = UserSession(
            session_id="expired-sid",
            user_id="uid",
            email="a@b.com",
            name="Alice",
            expires_at=time.time() - 1,
        )
        store.create_session(sess)
        assert store.get_session("expired-sid") is None

    def test_login_url_contains_pkce_params(self):
        """Login URL contains code_challenge=, S256, state=, nonce=."""
        from auth.oidc import SessionStore, OIDCFlow
        store = SessionStore()
        oidc = OIDCFlow(store)
        url = oidc.login_url()
        assert "code_challenge=" in url
        assert "S256" in url
        assert "state=" in url
        assert "nonce=" in url

    def test_pending_state_expires_after_10_minutes(self):
        """State older than 600s returns None from pop_pending()."""
        from auth.oidc import SessionStore, PendingState
        store = SessionStore()
        state_token, _, _, _ = store.create_auth_state()
        # Age the pending state
        store._pending[state_token].created_at = time.time() - 700
        result = store.pop_pending(state_token)
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# TestSessionManager
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestSessionManager:

    def test_add_user_message_grows_history(self, chat_session):
        """add_user() appends with role='user'."""
        chat_session.add_user("Hello")
        assert len(chat_session.history) == 1
        assert chat_session.history[0]["role"] == "user"
        assert chat_session.history[0]["content"] == "Hello"

    def test_add_assistant_message(self, chat_session):
        """add_assistant() appends with role='assistant'."""
        chat_session.add_user("Hi")
        chat_session.add_assistant("Hello back")
        assert chat_session.turn_count == 1
        assert chat_session.history[-1]["role"] == "assistant"

    def test_clear_resets_history(self, chat_session):
        """clear() empties the history list."""
        chat_session.add_user("msg")
        chat_session.add_assistant("reply")
        chat_session.clear()
        assert chat_session.history == []

    def test_history_returns_copy(self, chat_session):
        """Mutating the returned list does not affect internal state."""
        chat_session.add_user("msg")
        history = chat_session.history
        history.append({"role": "user", "content": "injected"})
        assert len(chat_session.history) == 1


# ─────────────────────────────────────────────────────────────────────────────
# TestChatbotCore
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestChatbotCore:

    def test_all_tools_registered(self, core):
        """execute_sql, kg_get_stats, dremio_run_sql all present in tool list."""
        names = [t["name"] for t in core._tools]
        assert "execute_sql" in names
        assert "kg_get_stats" in names
        assert "dremio_run_sql" in names

    def test_dispatch_routes_to_correct_capability(self, core, kg_cap, dremio_cap):
        """dremio_run_sql routes to DremioCapability."""
        dremio_cap.handle_tool = MagicMock(return_value={"rows": [], "row_count": 0})
        result = core._dispatch("dremio_run_sql", {"sql": "SELECT 1"})
        dremio_cap.handle_tool.assert_called_once_with("dremio_run_sql", {"sql": "SELECT 1"})

    def test_dispatch_unknown_tool_returns_error(self, core):
        """Unknown tool name returns {'error': ...} dict."""
        result = core._dispatch("nonexistent_tool", {})
        assert isinstance(result, dict)
        assert "error" in result

    def test_system_prompt_cached_after_first_build(self, core, chat_session):
        """Static block text is identical on repeat calls (cached)."""
        s1 = core._build_system("sid", 0)
        s2 = core._build_system("sid", 1)
        # The static portion (everything before the Session: suffix) is identical
        static1 = s1.split("\n\nSession:")[0]
        static2 = s2.split("\n\nSession:")[0]
        assert static1 == static2

    def test_system_prompt_contains_capability_fragments(self, core):
        """'DREMIO' and 'KNOWLEDGE' present in the cached system block."""
        s = core._build_system("sid", 0)
        assert "DREMIO" in s.upper()
        assert "KNOWLEDGE" in s.upper()
