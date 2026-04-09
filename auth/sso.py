"""
auth/sso.py — Entra ID M2M token manager (client_credentials / 2LO)
=====================================================================
Fetches and caches Entra ID access tokens using the OAuth 2.0
client_credentials grant (machine-to-machine, no user interaction).

Two pre-configured token managers:
  get_mcp_token_manager()        KB MCP server 2LO tokens
                                 scope: api://{ENTRA_MCP_API_URI}/.default
  get_ai_gateway_token_manager() Azure AI Foundry gateway M2M tokens
                                 scope: ENTRA_AI_SCOPE (env var)

Thread-safe; tokens are auto-refreshed 60s before expiry.

Required env vars:
  ENTRA_TENANT_ID      Azure AD tenant ID
  ENTRA_CLIENT_ID      Chatbot app registration client ID
  ENTRA_CLIENT_SECRET  Chatbot app client secret
  ENTRA_MCP_API_URI    KB MCP server identifier URI (e.g. api://kg-mcp-server-azure-dev)
  ENTRA_AI_SCOPE       (optional) AI Foundry gateway scope
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("auth.sso")

# ── Entra ID credentials ───────────────────────────────────────────────────────
ENTRA_TENANT_ID     = os.getenv("ENTRA_TENANT_ID",     "")
ENTRA_CLIENT_ID     = os.getenv("ENTRA_CLIENT_ID",     "")
ENTRA_CLIENT_SECRET = os.getenv("ENTRA_CLIENT_SECRET", "")
ENTRA_MCP_API_URI   = os.getenv("ENTRA_MCP_API_URI",   "")   # identifier URI of KB MCP server
ENTRA_AI_SCOPE      = os.getenv("ENTRA_AI_SCOPE",
    "https://cognitiveservices.azure.com/.default")

ENTRA_TOKEN_URL     = (f"https://login.microsoftonline.com"
                       f"/{ENTRA_TENANT_ID}/oauth2/v2.0/token")


@dataclass
class _CachedToken:
    access_token: str
    expires_at:   float   # unix timestamp


class EntraTokenManager:
    """
    Thread-safe Entra ID M2M token manager (client_credentials grant).
    Automatically refreshes the token 60 seconds before expiry.
    """

    _REFRESH_BUFFER = 60

    def __init__(self,
                 scope:         str = "",
                 tenant_id:     str = ENTRA_TENANT_ID,
                 client_id:     str = ENTRA_CLIENT_ID,
                 client_secret: str = ENTRA_CLIENT_SECRET):
        self._token_url     = (f"https://login.microsoftonline.com"
                               f"/{tenant_id}/oauth2/v2.0/token")
        self._client_id     = client_id
        self._client_secret = client_secret
        self._scope         = scope
        self._cached:  _CachedToken | None = None
        self._lock     = threading.Lock()

    def get_token(self) -> str:
        """Return a valid access token, fetching a new one if needed."""
        with self._lock:
            if self._cached and time.time() < self._cached.expires_at:
                return self._cached.access_token
            return self._refresh()

    def _refresh(self) -> str:
        """Fetch a new token from Entra ID. Called inside _lock."""
        if not self._scope:
            raise RuntimeError("EntraTokenManager: scope is not configured")
        if not self._client_id or not self._client_secret:
            raise RuntimeError("EntraTokenManager: ENTRA_CLIENT_ID / ENTRA_CLIENT_SECRET not set")

        payload = urllib.parse.urlencode({
            "grant_type":    "client_credentials",
            "client_id":     self._client_id,
            "client_secret": self._client_secret,
            "scope":         self._scope,
        }).encode()

        req = urllib.request.Request(
            self._token_url, data=payload, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"})

        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                resp = json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:300]
            raise RuntimeError(f"Entra ID token fetch failed HTTP {e.code}: {body}")
        except Exception as e:
            raise RuntimeError(f"Entra ID token fetch failed: {e}")

        token      = resp.get("access_token")
        expires_in = resp.get("expires_in", 3600)

        if not token:
            raise RuntimeError(f"Entra ID returned no access_token: {resp}")

        self._cached = _CachedToken(
            access_token = token,
            expires_at   = time.time() + expires_in - self._REFRESH_BUFFER,
        )
        log.info("Entra ID M2M token refreshed (scope=%s, expires_in=%ds)",
                 self._scope, expires_in)
        return token

    # Allow gateway to force a refresh on 401
    def _invalidate(self):
        with self._lock:
            self._cached = None

    def startup_check(self) -> tuple[bool, str]:
        """Verify Entra ID connectivity at startup."""
        try:
            tok = self.get_token()
            return True, f"Entra ID SSO OK (scope={self._scope}, token_len={len(tok)})"
        except Exception as e:
            return False, f"Entra ID SSO FAILED: {e}"


# ── Module-level singletons ───────────────────────────────────────────────────

_mcp_manager:        EntraTokenManager | None = None
_ai_gateway_manager: EntraTokenManager | None = None


def get_mcp_token_manager() -> EntraTokenManager:
    """Token manager for KB MCP server 2LO (machine-to-machine) calls."""
    global _mcp_manager
    if _mcp_manager is None:
        scope = f"{ENTRA_MCP_API_URI}/.default" if ENTRA_MCP_API_URI else ""
        _mcp_manager = EntraTokenManager(scope=scope)
    return _mcp_manager


def get_ai_gateway_token_manager() -> EntraTokenManager:
    """Token manager for Azure AI Foundry gateway M2M calls."""
    global _ai_gateway_manager
    if _ai_gateway_manager is None:
        _ai_gateway_manager = EntraTokenManager(scope=ENTRA_AI_SCOPE)
    return _ai_gateway_manager


def get_token_manager() -> EntraTokenManager:
    """Alias for AI gateway token manager (backward-compatible)."""
    return get_ai_gateway_token_manager()


def get_access_token() -> str:
    """Convenience: get AI gateway token."""
    return get_token_manager().get_token()
