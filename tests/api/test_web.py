"""
tests/api/test_web.py — FastAPI route and WebSocket tests.

Covers:
  - Auth routes: /auth/login, /auth/callback, /auth/me, /auth/logout
  - Protected routes: /, /api/health, /api/sessions
  - WebSocket protocol: ping/pong, reset, question → thinking → answer
  - Session isolation
"""
from __future__ import annotations

import json
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FORBIDDEN_WORDS


# ─────────────────────────────────────────────────────────────────────────────
# TestAuthRoutes
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.api
class TestAuthRoutes:

    def test_login_redirects_to_microsoft(self, client):
        """GET /auth/login → 302 to login.microsoftonline.com with PKCE params."""
        resp = client.get("/auth/login", follow_redirects=False)
        assert resp.status_code == 302
        location = resp.headers["location"]
        assert "login.microsoftonline.com" in location
        assert "code_challenge=" in location
        assert "S256" in location

    def test_login_with_return_to(self, client):
        """GET /auth/login?return_to=/dashboard → 302 (return_to preserved in state)."""
        resp = client.get("/auth/login?return_to=/dashboard", follow_redirects=False)
        assert resp.status_code == 302
        assert "login.microsoftonline.com" in resp.headers["location"]

    def test_callback_missing_code_redirects_to_login(self, client):
        """GET /auth/callback (no params) → 302 to /auth/login."""
        resp = client.get("/auth/callback", follow_redirects=False)
        assert resp.status_code == 302
        assert "/auth/login" in resp.headers["location"]

    def test_callback_with_error_returns_400(self, client):
        """GET /auth/callback?error=access_denied → 400."""
        resp = client.get(
            "/auth/callback?error=access_denied&error_description=User+denied",
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_callback_invalid_state_returns_error(self, client):
        """GET /auth/callback?code=abc&state=invalid → 302 or 500 (CSRF protection)."""
        resp = client.get(
            "/auth/callback?code=abc&state=totallyinvalidstate",
            follow_redirects=False,
        )
        assert resp.status_code in (302, 500)

    def test_me_unauthenticated_returns_401(self, client):
        """GET /auth/me (no cookie) → 401 {'authenticated': false}."""
        resp = client.get("/auth/me")
        assert resp.status_code == 401
        assert resp.json()["authenticated"] is False

    def test_me_authenticated_returns_user_info(self, authed_client):
        """GET /auth/me (with session cookie) → 200 with email + name."""
        resp = authed_client.get("/auth/me")
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is True
        assert "email" in data
        assert "name" in data

    def test_me_does_not_expose_access_token(self, authed_client):
        """GET /auth/me must not expose access_token or Bearer in the response body."""
        resp = authed_client.get("/auth/me")
        body = resp.text
        assert "access_token" not in body
        assert "Bearer" not in body

    def test_logout_clears_cookie(self, authed_client):
        """GET /auth/logout → 302 + session cookie cleared."""
        resp = authed_client.get("/auth/logout", follow_redirects=False)
        assert resp.status_code == 302
        # Cookie should be cleared (Max-Age=0 or deleted)
        set_cookie = resp.headers.get("set-cookie", "")
        assert "chatbot_session" in set_cookie or "microsoftonline" in resp.headers["location"]


# ─────────────────────────────────────────────────────────────────────────────
# TestProtectedRoutes
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.api
class TestProtectedRoutes:

    def test_root_unauthenticated_redirects(self, client):
        """GET / (no cookie) → 302 → /auth/login."""
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert "/auth/login" in resp.headers["location"]

    def test_root_authenticated_returns_200(self, authed_client):
        """GET / (with session) → 200 HTML."""
        resp = authed_client.get("/")
        assert resp.status_code == 200

    def test_health_unauthenticated_returns_401(self, client):
        """GET /api/health (no cookie) → 401."""
        resp = client.get("/api/health")
        assert resp.status_code == 401

    def test_health_authenticated_returns_ok(self, authed_client):
        """GET /api/health (with session) → 200 {'status': 'ok'}."""
        resp = authed_client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert "capabilities" in resp.json()

    def test_sessions_unauthenticated_returns_401(self, client):
        """GET /api/sessions (no cookie) → 401."""
        resp = client.get("/api/sessions")
        assert resp.status_code == 401

    def test_sessions_authenticated_returns_list(self, authed_client):
        """GET /api/sessions (with session) → 200 with sessions key."""
        resp = authed_client.get("/api/sessions")
        assert resp.status_code == 200
        assert "sessions" in resp.json()


# ─────────────────────────────────────────────────────────────────────────────
# TestWebSocketProtocol
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.api
class TestWebSocketProtocol:

    def test_ws_rejects_without_session_cookie(self, client):
        """WebSocket without a session cookie is rejected."""
        with pytest.raises(Exception):
            with client.websocket_connect(f"/ws/{uuid.uuid4()}") as ws:
                ws.send_json({"type": "ping"})

    def test_ws_ping_pong(self, authed_client):
        """WebSocket {'type': 'ping'} → {'type': 'pong'}."""
        chat_id = str(uuid.uuid4())
        with authed_client.websocket_connect(f"/ws/{chat_id}") as ws:
            ws.send_json({"type": "ping"})
            msg = ws.receive_json()
        assert msg["type"] == "pong"

    def test_ws_reset_generates_new_chat_id(self, authed_client):
        """WebSocket {'type': 'reset'} → {'type': 'reset', 'chat_id': '<uuid>'}."""
        chat_id = str(uuid.uuid4())
        with authed_client.websocket_connect(f"/ws/{chat_id}") as ws:
            ws.send_json({"type": "reset"})
            msg = ws.receive_json()
        assert msg["type"] == "reset"
        assert "chat_id" in msg

    def test_ws_question_produces_answer(self, authed_client, core):
        """WebSocket question → receives at least a 'thinking' or 'answer' event."""
        core._llm = MagicMock(return_value={
            "choices": [{"message": {"role": "assistant", "content": "Test answer"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10},
        })
        chat_id = str(uuid.uuid4())
        received = []
        with authed_client.websocket_connect(f"/ws/{chat_id}") as ws:
            ws.send_json({"type": "question", "text": "What is a cost center?"})
            for _ in range(10):
                try:
                    msg = ws.receive_json(mode="text")
                    received.append(msg)
                    if msg.get("type") in ("answer", "error"):
                        break
                except Exception:
                    break

        types = [m.get("type") for m in received]
        assert "thinking" in types or "answer" in types


# ─────────────────────────────────────────────────────────────────────────────
# TestSessionIsolation
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.api
class TestSessionIsolation:

    def test_different_users_have_different_sessions(self):
        """Two session IDs have independent UserSession objects."""
        from auth.oidc import SessionStore, UserSession
        store = SessionStore()

        s1 = UserSession(
            session_id="sid-1", user_id="u1", email="u1@x.com",
            name="User1", expires_at=time.time() + 3600,
        )
        s2 = UserSession(
            session_id="sid-2", user_id="u2", email="u2@x.com",
            name="User2", expires_at=time.time() + 3600,
        )
        store.create_session(s1)
        store.create_session(s2)

        assert store.get_session("sid-1").email == "u1@x.com"
        assert store.get_session("sid-2").email == "u2@x.com"

    def test_delete_session_removes_it(self):
        """delete_session() causes get_session() to return None."""
        from auth.oidc import SessionStore, UserSession
        store = SessionStore()
        sess = UserSession(
            session_id="to-delete", user_id="u", email="u@x.com",
            name="U", expires_at=time.time() + 3600,
        )
        store.create_session(sess)
        store.delete_session("to-delete")
        assert store.get_session("to-delete") is None
