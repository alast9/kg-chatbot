"""
auth/oidc.py — Entra ID (Azure AD) OIDC (authorization_code + PKCE) for the web app
=====================================================================================
Implements the full OIDC authorization_code + PKCE flow against Entra ID:
  1. /auth/login   → build Entra ID authorize URL with PKCE code_challenge
  2. /auth/callback → exchange code for tokens, verify JWT, create session
  3. /auth/logout  → revoke session, redirect to Entra ID logout

Session storage:
  Server-side dict (in-memory, single-process).
  Browser gets only an opaque HttpOnly + Secure session cookie.
  Entra ID tokens never leave the server.

Token forwarding for Snowflake / Dremio (3LO):
  The user's access_token is stored per-session and forwarded to the
  Azure AI Foundry Agent Service so user identity flows to the data layer.

JWT verification:
  Entra ID public keys fetched from the tenant JWKS endpoint and cached.
  Verification is local — no Entra ID round-trip per request.

Required env vars:
  ENTRA_TENANT_ID      Azure AD tenant ID
  ENTRA_CLIENT_ID      Chatbot app registration client ID
  ENTRA_CLIENT_SECRET  Chatbot app client secret
  APP_BASE_URL         Public base URL of this web app
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
from typing import Any

log = logging.getLogger("auth.oidc")

# ── Entra ID (Azure AD) config ────────────────────────────────────────────────
ENTRA_TENANT_ID     = os.getenv("ENTRA_TENANT_ID",     "")
ENTRA_CLIENT_ID     = os.getenv("ENTRA_CLIENT_ID",     "")
ENTRA_CLIENT_SECRET = os.getenv("ENTRA_CLIENT_SECRET", "")

_BASE               = f"https://login.microsoftonline.com/{ENTRA_TENANT_ID}/oauth2/v2.0"
ENTRA_AUTHORIZE_URL = f"{_BASE}/authorize"
ENTRA_TOKEN_URL     = f"{_BASE}/token"
ENTRA_LOGOUT_URL    = f"{_BASE}/logout"
ENTRA_JWKS_URL      = (f"https://login.microsoftonline.com"
                       f"/{ENTRA_TENANT_ID}/discovery/v2.0/keys")
ENTRA_ISSUER        = f"https://login.microsoftonline.com/{ENTRA_TENANT_ID}/v2.0"

# Scopes for user login.
# Include offline_access for refresh tokens.
# user_impersonation on cognitiveservices lets the access_token reach
# Azure AI Foundry for 3LO Snowflake/Dremio calls.
ENTRA_OIDC_SCOPES   = os.getenv(
    "ENTRA_OIDC_SCOPES",
    "openid profile email offline_access",
)

APP_BASE_URL = os.getenv("APP_BASE_URL", "https://localhost:8443")
CALLBACK_URI = f"{APP_BASE_URL}/auth/callback"

# Snowflake External OAuth — Entra ID app that represents Snowflake as a resource.
# Tokens issued for this scope are accepted by Snowflake's External OAuth integration.
SNOWFLAKE_ENTRA_APP_ID = os.getenv(
    "SNOWFLAKE_ENTRA_APP_ID", "5daaa11c-aff1-48ac-b265-d6fc645bc669")
SNOWFLAKE_ENTRA_SCOPE  = f"api://{SNOWFLAKE_ENTRA_APP_ID}/session:role:DEMO_READER offline_access"

# Dremio External Token Provider — chatbot app exposes Dremio.Access scope.
# Entra issues a token with aud = ENTRA_CLIENT_ID, which Dremio validates.
DREMIO_ENTRA_SCOPE = f"api://{ENTRA_CLIENT_ID}/Dremio.Access offline_access"

SESSION_TTL  = int(os.getenv("SESSION_TTL", "28800"))   # 8 hours
COOKIE_NAME  = "chatbot_session"


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class UserSession:
    """All auth state for one logged-in user."""
    session_id:       str
    user_id:          str                  # Entra ID object ID (sub)
    email:            str
    name:             str
    picture:          str = ""
    access_token:     str = ""             # forwarded to AI Foundry gateway
    id_token:         str = ""
    refresh_token:    str = ""
    snowflake_token:  str = ""             # Entra-issued token for Snowflake External OAuth
    snowflake_token_expires_at: float = 0.0
    dremio_token:     str = ""             # Dremio OAuth access token (from /auth/dremio/connect)
    dremio_token_expires_at: float = 0.0
    created_at:       float = field(default_factory=time.time)
    expires_at:       float = 0.0
    chat_session_id:  str = ""

    @property
    def valid(self) -> bool:
        return time.time() < self.expires_at

    def to_ui_dict(self) -> dict:
        """Safe subset for sending to the browser UI."""
        return {"email": self.email, "name": self.name, "picture": self.picture}


@dataclass
class PendingState:
    """PKCE state stored during the auth flow (before callback)."""
    code_verifier: str
    nonce:         str
    return_to:     str = "/"
    created_at:    float = field(default_factory=time.time)


# ── PKCE helpers ──────────────────────────────────────────────────────────────

def _pkce_pair() -> tuple[str, str]:
    """Generate (code_verifier, code_challenge) for PKCE S256."""
    verifier  = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    digest    = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ── JWKS / JWT verification ───────────────────────────────────────────────────

class JWKSCache:
    """Fetch and cache Entra ID public keys for local JWT verification."""

    def __init__(self, jwks_url: str):
        self._url  = jwks_url
        self._keys: dict[str, Any] = {}
        self._fetched_at: float = 0.0
        self._ttl  = 3600
        self._lock = threading.Lock()

    def get_public_key(self, kid: str) -> dict | None:
        with self._lock:
            if time.time() - self._fetched_at > self._ttl or not self._keys:
                self._refresh()
            return self._keys.get(kid)

    def _refresh(self):
        try:
            with urllib.request.urlopen(self._url, timeout=10) as r:
                data = json.loads(r.read())
            self._keys = {k["kid"]: k for k in data.get("keys", [])}
            self._fetched_at = time.time()
            log.info("JWKS refreshed: %d keys", len(self._keys))
        except Exception as e:
            log.warning("JWKS refresh failed: %s", e)


_jwks = JWKSCache(ENTRA_JWKS_URL)


def _b64decode_padding(s: str) -> bytes:
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def verify_id_token(id_token: str) -> dict:
    """
    Verify Entra ID id_token JWT.
    Returns claims dict on success; raises ValueError on failure.

    Uses PyJWT if available (recommended), falls back to manual decode.
    """
    try:
        import jwt as pyjwt
        header = json.loads(_b64decode_padding(id_token.split(".")[0]))
        jwk    = _jwks.get_public_key(header.get("kid", ""))
        if not jwk:
            raise ValueError("Unknown JWT kid — JWKS may need refresh")
        from jwt.algorithms import RSAAlgorithm
        pub_key = RSAAlgorithm.from_jwk(json.dumps(jwk))
        claims  = pyjwt.decode(
            id_token, pub_key,
            algorithms=["RS256"],
            audience=ENTRA_CLIENT_ID,
            issuer=ENTRA_ISSUER,
        )
        return claims
    except ImportError:
        log.warning("PyJWT not installed — JWT signature NOT verified (dev mode)")
        payload = json.loads(_b64decode_padding(id_token.split(".")[1]))
        if payload.get("exp", 0) < time.time():
            raise ValueError("JWT expired")
        return payload


# ── HTTP helper ───────────────────────────────────────────────────────────────

def _post_form(url: str, data: dict) -> dict:
    """POST application/x-www-form-urlencoded (required by Entra ID token endpoint)."""
    payload = urllib.parse.urlencode(data).encode()
    req     = urllib.request.Request(url, data=payload, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


# ── Snowflake token exchange ──────────────────────────────────────────────────

def exchange_snowflake_token(refresh_token: str) -> tuple[str, float]:
    """
    Use the user's Entra ID refresh_token to get an access token scoped for
    the Snowflake Entra app (api://<SF_APP_ID>/snowflake.query).
    Snowflake's External OAuth integration accepts this token as a Bearer token.
    Returns (snowflake_access_token, expires_at_unix_timestamp).
    """
    try:
        tokens = _post_form(ENTRA_TOKEN_URL, {
            "grant_type":    "refresh_token",
            "client_id":     ENTRA_CLIENT_ID,
            "client_secret": ENTRA_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "scope":         SNOWFLAKE_ENTRA_SCOPE,
        })
        if "error" in tokens:
            log.warning("Snowflake token exchange error: %s — %s",
                        tokens["error"], tokens.get("error_description", ""))
            return "", 0.0
        tok = tokens.get("access_token", "")
        exp = time.time() + tokens.get("expires_in", 3600) - 60   # 60s buffer
        log.info("Snowflake SSO token obtained (expires_in=%ds)", tokens.get("expires_in", 0))
        return tok, exp
    except Exception as e:
        log.warning("Snowflake token exchange failed: %s", e)
        return "", 0.0


def exchange_dremio_entra_token(refresh_token: str) -> tuple[str, float]:
    """
    Use the user's Entra ID refresh_token to get an access token scoped for
    the chatbot app's Dremio.Access scope (api://<ENTRA_CLIENT_ID>/Dremio.Access).
    Dremio's External Token Provider validates this token — aud = ENTRA_CLIENT_ID.
    Returns (dremio_access_token, expires_at_unix_timestamp).
    """
    if not DREMIO_ENTRA_SCOPE or not ENTRA_CLIENT_ID:
        return "", 0.0
    try:
        tokens = _post_form(ENTRA_TOKEN_URL, {
            "grant_type":    "refresh_token",
            "client_id":     ENTRA_CLIENT_ID,
            "client_secret": ENTRA_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "scope":         DREMIO_ENTRA_SCOPE,
        })
        if "error" in tokens:
            log.warning("Dremio Entra token exchange error: %s — %s",
                        tokens["error"], tokens.get("error_description", ""))
            return "", 0.0
        tok = tokens.get("access_token", "")
        exp = time.time() + tokens.get("expires_in", 3600) - 60   # 60s buffer
        log.info("Dremio Entra token obtained (expires_in=%ds)", tokens.get("expires_in", 0))
        return tok, exp
    except Exception as e:
        log.warning("Dremio Entra token exchange failed: %s", e)
        return "", 0.0


# ── Server-side session store ─────────────────────────────────────────────────

class SessionStore:
    """
    Thread-safe in-memory session store.
    Maps opaque session_id → UserSession.
    Expires old sessions automatically on access.
    """

    def __init__(self):
        self._sessions: dict[str, UserSession]  = {}
        self._pending:  dict[str, PendingState] = {}
        self._lock = threading.Lock()

    # ── Pending PKCE states ───────────────────────────────────────────────────

    def create_auth_state(self, return_to: str = "/") -> tuple[str, str, str, str]:
        """
        Create PKCE state for one authorization flow.
        Returns (state_token, code_verifier, code_challenge, nonce).
        """
        state_token   = secrets.token_urlsafe(32)
        code_verifier, code_challenge = _pkce_pair()
        nonce         = secrets.token_urlsafe(16)
        with self._lock:
            self._pending[state_token] = PendingState(
                code_verifier=code_verifier,
                nonce=nonce,
                return_to=return_to,
            )
        return state_token, code_verifier, code_challenge, nonce

    def pop_pending(self, state_token: str) -> PendingState | None:
        with self._lock:
            p = self._pending.pop(state_token, None)
        if p and time.time() - p.created_at > 600:
            return None   # stale
        return p

    # ── User sessions ─────────────────────────────────────────────────────────

    def create_session(self, session: UserSession) -> str:
        with self._lock:
            self._sessions[session.session_id] = session
            self._gc()
        return session.session_id

    def get_session(self, session_id: str) -> UserSession | None:
        with self._lock:
            s = self._sessions.get(session_id)
        if s and s.valid:
            return s
        if s:
            with self._lock:
                self._sessions.pop(session_id, None)
        return None

    def delete_session(self, session_id: str):
        with self._lock:
            self._sessions.pop(session_id, None)

    def _gc(self):
        """Remove expired sessions (called inside lock)."""
        now     = time.time()
        expired = [k for k, v in self._sessions.items() if now >= v.expires_at]
        for k in expired:
            del self._sessions[k]


# ── OIDC flow ─────────────────────────────────────────────────────────────────

class OIDCFlow:
    """
    Orchestrates the Entra ID authorization_code + PKCE flow.
    Used by the FastAPI route handlers in interfaces/web.py.
    """

    def __init__(self, store: SessionStore):
        self._store = store

    def login_url(self, return_to: str = "/") -> str:
        """
        Build the Entra ID authorize URL.
        Stores PKCE verifier and nonce server-side keyed by state token.
        """
        state, _, challenge, nonce = self._store.create_auth_state(return_to)
        params = {
            "client_id":             ENTRA_CLIENT_ID,
            "response_type":         "code",
            "redirect_uri":          CALLBACK_URI,
            "scope":                 ENTRA_OIDC_SCOPES,
            "state":                 state,
            "nonce":                 nonce,
            "code_challenge":        challenge,
            "code_challenge_method": "S256",
            "response_mode":         "query",
        }
        return f"{ENTRA_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    def handle_callback(self, code: str, state: str) -> UserSession:
        """
        Exchange authorization code for tokens.
        Verify id_token JWT. Create and store session.
        Returns UserSession on success; raises RuntimeError on failure.
        """
        pending = self._store.pop_pending(state)
        if not pending:
            raise RuntimeError("Invalid or expired state parameter")

        try:
            tokens = _post_form(ENTRA_TOKEN_URL, {
                "grant_type":    "authorization_code",
                "client_id":     ENTRA_CLIENT_ID,
                "client_secret": ENTRA_CLIENT_SECRET,
                "code":          code,
                "redirect_uri":  CALLBACK_URI,
                "code_verifier": pending.code_verifier,
                "scope":         ENTRA_OIDC_SCOPES,
            })
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:300]
            raise RuntimeError(f"Token exchange failed HTTP {e.code}: {body}")

        if "error" in tokens:
            raise RuntimeError(
                f"Token error: {tokens['error']}: {tokens.get('error_description', '')}")

        id_token      = tokens.get("id_token",      "")
        access_token  = tokens.get("access_token",  "")
        refresh_token = tokens.get("refresh_token", "")
        expires_in    = tokens.get("expires_in",    SESSION_TTL)

        try:
            claims = verify_id_token(id_token)
        except Exception as e:
            raise RuntimeError(f"JWT verification failed: {e}")

        # Silently exchange the refresh_token for data-platform tokens at login.
        # This eliminates separate /auth/snowflake and /auth/dremio connect steps.
        sf_token,  sf_expires  = exchange_snowflake_token(refresh_token)  if refresh_token else ("", 0.0)
        dremio_tok, dremio_exp = exchange_dremio_entra_token(refresh_token) if refresh_token else ("", 0.0)

        session = UserSession(
            session_id       = secrets.token_urlsafe(32),
            user_id          = claims.get("sub",   ""),
            email            = claims.get("email",
                               claims.get("preferred_username", "")),
            name             = claims.get("name",
                               claims.get("email",
                               claims.get("preferred_username", "User"))),
            picture          = claims.get("picture", ""),
            access_token     = access_token,
            id_token         = id_token,
            refresh_token    = refresh_token,
            snowflake_token  = sf_token,
            snowflake_token_expires_at = sf_expires,
            dremio_token               = dremio_tok,
            dremio_token_expires_at    = dremio_exp,
            expires_at       = time.time() + min(expires_in, SESSION_TTL),
        )
        self._store.create_session(session)
        log.info("Session created: user=%s session=%s...",
                 session.email, session.session_id[:8])
        return session

    def logout_url(self, session_id: str) -> str:
        """Delete server-side session and return Entra ID logout URL."""
        self._store.delete_session(session_id)
        params = {
            "post_logout_redirect_uri": APP_BASE_URL,
        }
        return f"{ENTRA_LOGOUT_URL}?{urllib.parse.urlencode(params)}"


# ── Module-level singletons ───────────────────────────────────────────────────
_store = SessionStore()
_oidc  = OIDCFlow(_store)

def get_session_store() -> SessionStore: return _store
def get_oidc_flow()     -> OIDCFlow:     return _oidc
