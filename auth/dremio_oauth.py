"""
auth/dremio_oauth.py — Dremio Cloud OAuth 2.0 (authorization_code flow)
========================================================================
Dremio Cloud OAuth app (org-level) endpoints:
  Authorize:  https://app.dremio.cloud/oauth2/authorize
  Token:      https://api.dremio.cloud/v0/projects/{project_id}/oauth2/token
  Redirect:   <APP_BASE_URL>/auth/dremio/connect   (configured in Dremio OAuth app settings)

Flow:
  1. GET /auth/dremio/connect   (no params)    → redirect to Dremio authorize URL
  2. Dremio authenticates user → redirects to /auth/dremio/connect?code=...
  3. GET /auth/dremio/connect?code=...         → exchange code for token, store in session

Required env vars:
  DREMIO_OAUTH_CLIENT_ID      Client ID from Dremio OAuth app settings
  DREMIO_OAUTH_CLIENT_SECRET  Client secret from Dremio OAuth app settings
  DREMIO_PROJECT_ID           Dremio Cloud project UUID
  APP_BASE_URL                Public base URL (for redirect URI)

Future — SSO shortcut (like Snowflake):
  Configure Entra ID as SAML/OIDC IdP in Dremio Cloud org settings.
  Then exchange the user's Entra ID token for a Dremio token silently at chatbot
  login, eliminating the explicit /auth/dremio/connect step entirely.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
import urllib.error

log = logging.getLogger("auth.dremio")

DREMIO_CLIENT_ID     = os.getenv("DREMIO_OAUTH_CLIENT_ID",     "")
DREMIO_CLIENT_SECRET = os.getenv("DREMIO_OAUTH_CLIENT_SECRET", "")
DREMIO_PROJECT_ID    = os.getenv("DREMIO_PROJECT_ID",          "dea2a74c-2f8a-4eef-8d40-c87db48d79ff")
APP_BASE_URL         = os.getenv("APP_BASE_URL",               "https://localhost:8443")

DREMIO_AUTHORIZE_URL = "https://app.dremio.cloud/oauth2/authorize"
DREMIO_TOKEN_URL     = f"https://api.dremio.cloud/v0/projects/{DREMIO_PROJECT_ID}/oauth2/token"
DREMIO_REDIRECT_URI  = f"{APP_BASE_URL}/auth/dremio/connect"

# Scopes — Dremio Cloud uses "read" or "write" at the org level
# Leave blank to use Dremio's default scopes for the OAuth app
DREMIO_SCOPES = os.getenv("DREMIO_OAUTH_SCOPES", "")


def authorize_url() -> str:
    """Build the Dremio OAuth authorize URL to redirect the user to."""
    params = {
        "client_id":     DREMIO_CLIENT_ID,
        "response_type": "code",
        "redirect_uri":  DREMIO_REDIRECT_URI,
    }
    if DREMIO_SCOPES:
        params["scope"] = DREMIO_SCOPES
    return f"{DREMIO_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(code: str) -> tuple[str, float]:
    """
    Exchange an authorization code for an access token.
    Returns (access_token, expires_at_unix_timestamp).
    """
    payload = urllib.parse.urlencode({
        "grant_type":    "authorization_code",
        "client_id":     DREMIO_CLIENT_ID,
        "client_secret": DREMIO_CLIENT_SECRET,
        "code":          code,
        "redirect_uri":  DREMIO_REDIRECT_URI,
    }).encode()

    req = urllib.request.Request(
        DREMIO_TOKEN_URL,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            tokens = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode()[:300]
        except Exception: pass
        raise RuntimeError(f"Dremio token exchange HTTP {e.code}: {body}")

    if "error" in tokens:
        raise RuntimeError(
            f"Dremio token error: {tokens['error']}: {tokens.get('error_description', '')}")

    token      = tokens.get("access_token", "")
    expires_in = tokens.get("expires_in", 3600)
    expires_at = time.time() + expires_in - 60  # 60s buffer
    log.info("Dremio OAuth token obtained (expires_in=%ds)", expires_in)
    return token, expires_at


def is_configured() -> bool:
    """Return True if OAuth client credentials are set."""
    return bool(DREMIO_CLIENT_ID and DREMIO_CLIENT_SECRET)
