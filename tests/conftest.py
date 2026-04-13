"""
tests/conftest.py — Shared fixtures for all test layers.

All fixtures live here and are auto-available to every test module.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ── Forbidden words (must never appear in any response) ───────────────────────
FORBIDDEN_WORDS = [
    "Chatbot@Test123",
    "DmDoQPVveXf",
    "b3F0QlhwMEJ",
    "2OFAFNcjQ76",
    "alast9@bus28live",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_jwt(payload: dict | None = None) -> str:
    """Build a structurally valid (but unsigned) JWT for testing."""
    import base64
    header  = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT","kid":"test-kid"}').rstrip(b"=").decode()
    body    = payload or {
        "sub":   "test-user-oid",
        "email": "test@example.com",
        "name":  "Test User",
        "exp":   int(time.time()) + 3600,
        "aud":   "test-client-id",
        "iss":   "https://login.microsoftonline.com/test-tenant/v2.0",
    }
    payload_b64 = base64.urlsafe_b64encode(json.dumps(body).encode()).rstrip(b"=").decode()
    return f"{header}.{payload_b64}.fakesig"


def _openai_response(text: str, tool_calls: list | None = None) -> dict:
    """Build a minimal OpenAI chat completion response dict."""
    finish = "tool_calls" if tool_calls else "stop"
    msg: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "choices": [{"message": msg, "finish_reason": finish}],
        "usage":   {"prompt_tokens": 100, "completion_tokens": 20},
    }


# ── Auth fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def mock_entra_token() -> str:
    """A structurally valid (unsigned) JWT for Entra ID tests."""
    return _make_jwt()


@pytest.fixture
def mock_token_manager(mock_entra_token):
    """EntraTokenManager with a pre-cached token — no network calls."""
    from auth.sso import EntraTokenManager, _CachedToken
    mgr = EntraTokenManager(
        scope="api://test-mcp/.default",
        tenant_id="test-tenant",
        client_id="test-client",
        client_secret="test-secret",
    )
    mgr._cached = _CachedToken(
        access_token=mock_entra_token,
        expires_at=time.time() + 3600,
    )
    return mgr


@pytest.fixture
def mock_gateway(mock_token_manager):
    """AgentCoreGatewayClient with canned Dremio results."""
    from auth.agentcore_gateway import AgentCoreGatewayClient
    gw = AgentCoreGatewayClient(
        gateway_url="https://mock-gateway.example.com/mcp",
        token_manager=mock_token_manager,
    )
    gw._tools_cache = [
        {"name": "RunSqlQuery",       "description": "Run SQL on Dremio"},
        {"name": "SearchTableAndViews", "description": "Search for tables"},
    ]
    gw.call_tool = MagicMock(return_value={"rows": [], "row_count": 0, "columns": []})
    gw.call_tool_with_token = MagicMock(return_value={"rows": [], "row_count": 0, "columns": []})
    gw.list_tools = MagicMock(return_value=gw._tools_cache)
    gw.startup_check = MagicMock(return_value=(True, "Mock gateway OK"))
    return gw


# ── Capability fixtures ───────────────────────────────────────────────────────

@pytest.fixture
def mock_neo4j():
    """Neo4j driver that returns an empty result list."""
    drv = MagicMock()
    session_ctx = MagicMock()
    session_ctx.__enter__ = MagicMock(return_value=session_ctx)
    session_ctx.__exit__ = MagicMock(return_value=False)
    session_ctx.run = MagicMock(return_value=[])
    drv.session = MagicMock(return_value=session_ctx)
    return drv


@pytest.fixture
def mock_es():
    """Elasticsearch client with empty search hits."""
    es = MagicMock()
    es.search = MagicMock(return_value={"hits": {"hits": []}})
    es.info   = MagicMock(return_value={"version": {"number": "8.0.0"}})
    return es


@pytest.fixture
def kg_cap(mock_neo4j, mock_es):
    """KnowledgeGraphCapability with mocked Neo4j + ES, MCP disabled."""
    with patch("capabilities.knowledge_graph.KnowledgeGraphCapability._init_neo4j"), \
         patch("capabilities.knowledge_graph.KnowledgeGraphCapability._init_es"), \
         patch("capabilities.knowledge_graph.KnowledgeGraphCapability._init_mcp"):
        from capabilities.knowledge_graph import KnowledgeGraphCapability
        cap = KnowledgeGraphCapability()
        cap._neo4j_driver = mock_neo4j
        cap._es_client    = mock_es
        cap._mcp_ok       = False
    return cap


@pytest.fixture
def dremio_cap():
    """DremioCapability with no external connections."""
    from capabilities.dremio import DremioCapability
    cap = DremioCapability()
    return cap


# ── LLM / Bedrock fixtures ────────────────────────────────────────────────────

@pytest.fixture
def mock_bedrock_response():
    """Canned OpenAI-format response with a simple text answer."""
    return _openai_response("This is a test answer.")


# ── Session fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def mock_mongo():
    """MongoHistory stub — in-memory only, no network."""
    m = MagicMock()
    m.ok = False
    m.load = MagicMock(return_value=[])
    m.append = MagicMock()
    m.list_sessions = MagicMock(return_value=[])
    m.delete_session = MagicMock()
    return m


@pytest.fixture
def mock_redis():
    """RedisWindow stub — in-memory only, no network."""
    r = MagicMock()
    r.ok = False
    r.push = MagicMock()
    r.get_recent = MagicMock(return_value=[])
    r.clear = MagicMock()
    return r


@pytest.fixture
def chat_session(mock_mongo, mock_redis):
    """SessionManager backed by mock persistence."""
    from session import SessionManager
    return SessionManager(
        session_id=str(uuid.uuid4()),
        mongo=mock_mongo,
        redis=mock_redis,
        capabilities=["knowledge_graph", "dremio", "snowflake"],
    )


@pytest.fixture
def core(kg_cap, dremio_cap, mock_bedrock_response):
    """ChatbotCore with mocked capabilities and a patched LLM."""
    from chatbot_core import ChatbotCore
    c = ChatbotCore([kg_cap, dremio_cap])
    c._llm = MagicMock(return_value=mock_bedrock_response)
    return c


# ── FastAPI app + client fixtures ─────────────────────────────────────────────

@pytest.fixture
def app(core, mock_mongo, mock_redis):
    """FastAPI app under test (static files not required)."""
    from interfaces.web import create_app
    return create_app(core, mock_mongo, mock_redis, ["knowledge_graph", "dremio"])


@pytest.fixture
def client(app):
    """Unauthenticated TestClient."""
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def session_cookie(app):
    """
    Injects a valid UserSession into the live session store and returns
    (cookie_name, session_id) so tests can authenticate requests.
    """
    from auth.oidc import get_session_store, UserSession, COOKIE_NAME
    store = get_session_store()
    sess = UserSession(
        session_id    = secrets_token(),
        user_id       = "test-oid-123",
        email         = "test@example.com",
        name          = "Test User",
        access_token  = "fake-access-token",
        id_token      = "fake-id-token",
        refresh_token = "fake-refresh-token",
        expires_at    = time.time() + 3600,
        chat_session_id = str(uuid.uuid4()),
    )
    store.create_session(sess)
    return COOKIE_NAME, sess.session_id


def secrets_token() -> str:
    import secrets
    return secrets.token_urlsafe(32)


@pytest.fixture
def authed_client(app, session_cookie):
    """TestClient pre-authenticated via an injected session cookie."""
    cookie_name, session_id = session_cookie
    client = TestClient(app, raise_server_exceptions=True)
    client.cookies.set(cookie_name, session_id)
    return client
