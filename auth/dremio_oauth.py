"""
auth/dremio_oauth.py — Dremio Cloud OAuth 2.0 (authorization_code + PKCE, Native app)
=======================================================================================
Dremio Cloud OAuth app (org-level, Native app type — no client secret):
  Authorize:  https://app.dremio.cloud/oauth2/authorize
  Token:      https://api.dremio.cloud/v0/projects/{project_id}/oauth2/token
  Redirect:   <APP_BASE_URL>/auth/dremio/connect   (configured in Dremio OAuth app settings)

Flow (PKCE — no client_secret required for Native apps):
  1. GET /auth/dremio/connect   (no params)    → redirect to Dremio authorize URL w/ PKCE challenge
  2. Dremio authenticates user → redirects to /auth/dremio/connect?code=...
  3. GET /auth/dremio/connect?code=...         → exchange code + verifier, store token in session

Required env vars:
  DREMIO_OAUTH_CLIENT_ID      Client ID from Dremio OAuth app settings
  DREMIO_PROJECT_ID           Dremio Cloud project UUID
  APP_BASE_URL                Public base URL (for redirect URI)
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.parse
import urllib.request
import urllib.error

log = logging.getLogger("auth.dremio")

DREMIO_CLIENT_ID  = os.getenv("DREMIO_OAUTH_CLIENT_ID", "")
DREMIO_PROJECT_ID = os.getenv("DREMIO_PROJECT_ID",       "dea2a74c-2f8a-4eef-8d40-c87db48d79ff")
APP_BASE_URL      = os.getenv("APP_BASE_URL",            "https://localhost:8443")

DREMIO_AUTHORIZE_URL = "https://app.dremio.cloud/oauth2/authorize"
DREMIO_TOKEN_URL     = f"https://api.dremio.cloud/v0/projects/{DREMIO_PROJECT_ID}/oauth2/token"
DREMIO_REDIRECT_URI  = f"{APP_BASE_URL}/auth/dremio/connect"

# Scopes — leave blank to use Dremio's default scopes for the OAuth app
DREMIO_SCOPES = os.getenv("DREMIO_OAUTH_SCOPES", "")

# In-memory PKCE state store: state_token → code_verifier
# (single-process; fine for the chatbot's scale)
_pending_verifiers: dict[str, str] = {}


def _pkce_pair() -> tuple[str, str]:
    """Generate (code_verifier, code_challenge) for PKCE S256."""
    verifier  = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    digest    = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url() -> str:
    """
    Build the Dremio OAuth authorize URL (PKCE).
    Stores the code_verifier in memory keyed by state token so exchange_code() can use it.
    """
    state_token        = secrets.token_urlsafe(32)
    code_verifier, code_challenge = _pkce_pair()
    _pending_verifiers[state_token] = code_verifier

    params = {
        "client_id":             DREMIO_CLIENT_ID,
        "response_type":         "code",
        "redirect_uri":          DREMIO_REDIRECT_URI,
        "code_challenge":        code_challenge,
        "code_challenge_method": "S256",
        "state":                 state_token,
    }
    if DREMIO_SCOPES:
        params["scope"] = DREMIO_SCOPES
    return f"{DREMIO_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(code: str, state: str = "") -> tuple[str, float]:
    """
    Exchange an authorization code for an access token using PKCE (no client_secret).
    Returns (access_token, expires_at_unix_timestamp).
    """
    # Retrieve the stored PKCE verifier (if state was passed through)
    code_verifier = _pending_verifiers.pop(state, "") if state else ""

    form: dict = {
        "grant_type":   "authorization_code",
        "client_id":    DREMIO_CLIENT_ID,
        "code":         code,
        "redirect_uri": DREMIO_REDIRECT_URI,
    }
    if code_verifier:
        form["code_verifier"] = code_verifier

    payload = urllib.parse.urlencode(form).encode()
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
    """Return True if OAuth client ID is set (no secret needed for Native apps)."""
    return bool(DREMIO_CLIENT_ID)
