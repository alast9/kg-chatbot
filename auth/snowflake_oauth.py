"""
auth/snowflake_oauth.py — Snowflake OAuth 3LO (authorization code grant + PKCE)
================================================================================
Implements per-user Snowflake OAuth against the DEMO_MCP_OAUTH security integration.

Flow:
  1. /auth/snowflake/connect  → login_url(user_id)  → redirect to Snowflake authorize
  2. Snowflake redirects back → /auth/snowflake/callback?code=...&state=...
  3. handle_callback(code, state) → exchanges code for access + refresh tokens
  4. get_token(user_id) → returns a valid access token (auto-refreshes via refresh token)

Tokens are stored in-process keyed by Entra user_id (sub). Entra identity is
only used as a session key — the Snowflake token belongs to whichever local
Snowflake user authenticated during the OAuth consent screen.

Required env vars:
  SNOWFLAKE_OAUTH_CLIENT_ID      Client ID from DEMO_MCP_OAUTH integration
  SNOWFLAKE_OAUTH_CLIENT_SECRET  Client secret from DEMO_MCP_OAUTH integration
  SNOWFLAKE_ACCOUNT              Account identifier  (e.g. XJSKMFC-WQC92044)
  APP_BASE_URL                   Public chatbot URL  (for redirect_uri construction)
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger("auth.snowflake")

SNOWFLAKE_ACCOUNT             = os.getenv("SNOWFLAKE_ACCOUNT", "XJSKMFC-WQC92044")
SNOWFLAKE_OAUTH_CLIENT_ID     = os.getenv("SNOWFLAKE_OAUTH_CLIENT_ID", "")
SNOWFLAKE_OAUTH_CLIENT_SECRET = os.getenv("SNOWFLAKE_OAUTH_CLIENT_SECRET", "")
APP_BASE_URL                  = os.getenv("APP_BASE_URL", "https://localhost:8443")

_BASE        = f"https://{SNOWFLAKE_ACCOUNT}.snowflakecomputing.com"
AUTH_URL     = f"{_BASE}/oauth/authorize"
TOKEN_URL    = f"{_BASE}/oauth/token-request"
REDIRECT_URI = f"{APP_BASE_URL}/auth/snowflake/callback"
# Grants the token the DEMO_READER default role
OAUTH_SCOPE  = "session:role:DEMO_READER"

_REFRESH_BUFFER = 60   # refresh access token 60 s before expiry
_STATE_TTL      = 300  # pending state expires after 5 min


def _pkce_pair() -> tuple[str, str]:
    verifier  = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    digest    = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


@dataclass
class _PendingState:
    code_verifier: str
    user_id:       str
    created_at:    float = field(default_factory=time.time)


@dataclass
class _SnowflakeToken:
    access_token:  str
    refresh_token: str
    expires_at:    float   # unix timestamp


class SnowflakeOAuthFlow:
    """Per-user Snowflake OAuth 3LO token manager. Thread-safe."""

    def __init__(self):
        self._pending: dict[str, _PendingState]   = {}  # state   → pending
        self._tokens:  dict[str, _SnowflakeToken] = {}  # user_id → token
        self._lock = threading.Lock()

    # ── Authorization URL ─────────────────────────────────────────────────────

    def login_url(self, user_id: str) -> str:
        """Build the Snowflake OAuth authorization URL for this user (PKCE S256)."""
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(24)
        with self._lock:
            self._pending[state] = _PendingState(
                code_verifier=verifier, user_id=user_id)
        params = urllib.parse.urlencode({
            "response_type":         "code",
            "client_id":             SNOWFLAKE_OAUTH_CLIENT_ID,
            "redirect_uri":          REDIRECT_URI,
            "state":                 state,
            "code_challenge":        challenge,
            "code_challenge_method": "S256",
            "scope":                 OAUTH_SCOPE,
        })
        return f"{AUTH_URL}?{params}"

    # ── Callback handling ─────────────────────────────────────────────────────

    def handle_callback(self, code: str, state: str) -> str:
        """
        Exchange authorization code for tokens.
        Returns the Entra user_id that initiated the flow.
        """
        with self._lock:
            pending = self._pending.pop(state, None)
        if not pending:
            raise ValueError("Invalid or expired OAuth state parameter")
        if time.time() - pending.created_at > _STATE_TTL:
            raise ValueError("OAuth state expired — please reconnect")

        access_token, refresh_token, expires_in = self._exchange(
            grant_type="authorization_code",
            code=code,
            code_verifier=pending.code_verifier,
        )
        with self._lock:
            self._tokens[pending.user_id] = _SnowflakeToken(
                access_token  = access_token,
                refresh_token = refresh_token,
                expires_at    = time.time() + expires_in - _REFRESH_BUFFER,
            )
        log.info("Snowflake token stored for user %s (expires_in=%ds)",
                 pending.user_id[:8], expires_in)
        return pending.user_id

    # ── Token access ──────────────────────────────────────────────────────────

    def get_token(self, user_id: str) -> str:
        """Return a valid Snowflake access token, auto-refreshing if needed."""
        with self._lock:
            tok = self._tokens.get(user_id)
        if not tok:
            raise RuntimeError(
                "Snowflake not connected — visit /auth/snowflake/connect")
        if time.time() < tok.expires_at:
            return tok.access_token
        if tok.refresh_token:
            return self._do_refresh(user_id, tok.refresh_token)
        raise RuntimeError(
            "Snowflake token expired and no refresh token — please reconnect")

    def is_connected(self, user_id: str) -> bool:
        with self._lock:
            return user_id in self._tokens

    def disconnect(self, user_id: str) -> None:
        with self._lock:
            self._tokens.pop(user_id, None)
        log.info("Snowflake disconnected for user %s", user_id[:8])

    # ── Token exchange helpers ────────────────────────────────────────────────

    def _exchange(self, grant_type: str, **extra) -> tuple[str, str, int]:
        """POST to Snowflake token URL. Returns (access_token, refresh_token, expires_in)."""
        payload = urllib.parse.urlencode(
            {"grant_type": grant_type, "redirect_uri": REDIRECT_URI, **extra}
        ).encode()
        creds = base64.b64encode(
            f"{SNOWFLAKE_OAUTH_CLIENT_ID}:{SNOWFLAKE_OAUTH_CLIENT_SECRET}".encode()
        ).decode()
        req = urllib.request.Request(
            TOKEN_URL, data=payload, method="POST",
            headers={
                "Content-Type":  "application/x-www-form-urlencoded",
                "Authorization": f"Basic {creds}",
            })
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                resp = json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:400]
            raise RuntimeError(f"Snowflake token exchange HTTP {e.code}: {body}")
        except Exception as e:
            raise RuntimeError(f"Snowflake token exchange failed: {e}")

        access_token  = resp.get("access_token")
        refresh_token = resp.get("refresh_token", "")
        expires_in    = int(resp.get("expires_in", 600))
        if not access_token:
            raise RuntimeError(f"Snowflake returned no access_token: {resp}")
        return access_token, refresh_token, expires_in

    def _do_refresh(self, user_id: str, refresh_token: str) -> str:
        access_token, new_refresh, expires_in = self._exchange(
            grant_type="refresh_token",
            refresh_token=refresh_token,
        )
        with self._lock:
            self._tokens[user_id] = _SnowflakeToken(
                access_token  = access_token,
                refresh_token = new_refresh or refresh_token,
                expires_at    = time.time() + expires_in - _REFRESH_BUFFER,
            )
        log.info("Snowflake token refreshed for user %s", user_id[:8])
        return access_token


# ── Module-level singleton ────────────────────────────────────────────────────

_flow: SnowflakeOAuthFlow | None = None


def get_snowflake_flow() -> SnowflakeOAuthFlow:
    global _flow
    if _flow is None:
        _flow = SnowflakeOAuthFlow()
    return _flow
