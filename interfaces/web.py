"""
interfaces/web.py — FastAPI web interface with Entra ID OIDC SSO
================================================================
Runs on port 8000 (TLS terminated by Azure Container Apps ingress).

Auth routes (no session required):
  GET  /auth/login      → redirect to Entra ID authorize URL (PKCE)
  GET  /auth/callback   → exchange code for tokens, set secure cookie
  GET  /auth/logout     → delete session, redirect to Entra ID logout
  GET  /auth/me         → current user info JSON (for UI bootstrap)

Protected routes (302 → /auth/login if no valid session cookie):
  GET  /                → index.html (chat UI)
  GET  /api/health      → capabilities status
  GET  /api/sessions    → recent chat sessions
  WS   /ws/{chat_id}   → WebSocket chat (cookie verified on upgrade)

Session cookie:
  Name:     chatbot_session
  HttpOnly: True  — JS cannot read
  Secure:   True  — HTTPS only
  SameSite: Lax   — CSRF protection
  Max-Age:  28800 — 8 hours

User-delegated Dremio access:
  Each WebSocket connection injects the user's access_token into the
  DremioCapability. The AgentCore gateway forwards it to Dremio, which
  applies per-user data access controls. Different users see different data.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from auth.oidc import (
    get_oidc_flow, get_session_store,
    COOKIE_NAME, SESSION_TTL, UserSession,
)

log = logging.getLogger("web")

STATIC_DIR = Path(__file__).parent.parent / "static"


# ── Cookie helpers ────────────────────────────────────────────────────────────

def _set_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME, value=session_id,
        httponly=True, secure=True, samesite="lax",
        max_age=SESSION_TTL, path="/",
    )


def _clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def _get_session(request: Request) -> UserSession | None:
    sid = request.cookies.get(COOKIE_NAME)
    return get_session_store().get_session(sid) if sid else None


# ── App factory ───────────────────────────────────────────────────────────────

def create_app(core, mongo, redis, cap_names: list[str]) -> FastAPI:
    app   = FastAPI(title="Multi-Capability Chatbot", version="2.0.0")
    oidc  = get_oidc_flow()
    store = get_session_store()

    _chat_sessions: dict[str, Any] = {}   # chat_id → SessionManager

    def _get_chat_session(chat_id: str) -> Any:
        from session import SessionManager
        if chat_id not in _chat_sessions:
            _chat_sessions[chat_id] = SessionManager(chat_id, mongo, redis, cap_names)
        return _chat_sessions[chat_id]

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ── Auth routes ───────────────────────────────────────────────────────────

    @app.get("/auth/login")
    async def auth_login(return_to: str = "/"):
        url = oidc.login_url(return_to=return_to)
        return RedirectResponse(url, status_code=302)

    @app.get("/auth/callback")
    async def auth_callback(request: Request,
                            code: str = None, state: str = None,
                            error: str = None, error_description: str = None):
        if error:
            return HTMLResponse(
                f"<h2>Login failed</h2><p>{error}: {error_description}</p>"
                f"<p><a href='/auth/login'>Try again</a></p>", status_code=400)
        if not code or not state:
            return RedirectResponse("/auth/login", status_code=302)
        try:
            user_session = await asyncio.to_thread(oidc.handle_callback, code, state)
        except Exception as e:
            log.error("Callback error: %s", e)
            return HTMLResponse(
                f"<h2>Auth error</h2><p>{e}</p>"
                f"<p><a href='/auth/login'>Try again</a></p>", status_code=500)
        user_session.chat_session_id = str(uuid.uuid4())
        store.create_session(user_session)
        response = RedirectResponse("/", status_code=302)
        _set_cookie(response, user_session.session_id)
        log.info("Login: %s", user_session.email)
        return response

    # ── Dremio OAuth (authorization_code flow) ────────────────────────────────

    @app.get("/auth/dremio/connect")
    async def dremio_connect(request: Request, code: str = None, error: str = None):
        """
        Dual-purpose endpoint:
          GET /auth/dremio/connect          → redirect to Dremio OAuth authorize URL
          GET /auth/dremio/connect?code=... → exchange code, store token, redirect to /
        """
        from auth.dremio_oauth import authorize_url, exchange_code, is_configured

        s = _get_session(request)
        if not s:
            return RedirectResponse("/auth/login", status_code=302)

        if error:
            return HTMLResponse(
                f"<h2>Dremio auth failed</h2><p>{error}</p>"
                f"<p><a href='/'>Back to chatbot</a></p>", status_code=400)

        if code:
            # Callback — exchange code for token
            if not is_configured():
                return HTMLResponse(
                    "<h2>Dremio OAuth not configured</h2>"
                    "<p>Set DREMIO_OAUTH_CLIENT_ID and DREMIO_OAUTH_CLIENT_SECRET.</p>",
                    status_code=500)
            try:
                token, expires_at = await asyncio.to_thread(exchange_code, code)
                s.dremio_token             = token
                s.dremio_token_expires_at  = expires_at
                log.info("Dremio OAuth connected: %s", s.email)
                return RedirectResponse("/", status_code=302)
            except Exception as e:
                log.error("Dremio token exchange failed: %s", e)
                return HTMLResponse(
                    f"<h2>Dremio auth error</h2><p>{e}</p>"
                    f"<p><a href='/auth/dremio/connect'>Try again</a></p>", status_code=500)

        # No code — initiate OAuth flow
        if not is_configured():
            return HTMLResponse(
                "<h2>Dremio OAuth not configured</h2>"
                "<p>Set DREMIO_OAUTH_CLIENT_ID and DREMIO_OAUTH_CLIENT_SECRET, "
                "or set DREMIO_PAT for service-account access.</p>",
                status_code=500)
        url = authorize_url()
        return RedirectResponse(url, status_code=302)

    @app.get("/auth/dremio/status")
    async def dremio_status(request: Request):
        """Return whether the session has a valid Dremio OAuth token."""
        s = _get_session(request)
        if not s:
            return JSONResponse({"connected": False, "reason": "not logged in"}, status_code=401)
        import os as _os
        pat_available = bool(_os.getenv("DREMIO_PAT"))
        connected = bool(s.dremio_token) and time.time() < s.dremio_token_expires_at
        return JSONResponse({
            "connected": connected or pat_available,
            "oauth": connected,
            "pat_fallback": pat_available and not connected,
        })

    # ── Snowflake SSO status (3LO replaced by Entra External OAuth) ──────────

    @app.get("/auth/snowflake/status")
    async def snowflake_status(request: Request):
        """Return whether the session has a valid Snowflake SSO token."""
        s = _get_session(request)
        if not s:
            return JSONResponse({"connected": False, "reason": "not logged in"}, status_code=401)
        connected = bool(s.snowflake_token) and time.time() < s.snowflake_token_expires_at
        return JSONResponse({"connected": connected, "sso": True})

    @app.get("/auth/logout")
    async def auth_logout(request: Request):
        sid = request.cookies.get(COOKIE_NAME)
        logout_url = await asyncio.to_thread(oidc.logout_url, sid) if sid else "/"
        resp = RedirectResponse(logout_url, status_code=302)
        _clear_cookie(resp)
        return resp

    @app.get("/auth/me")
    async def auth_me(request: Request):
        s = _get_session(request)
        if not s:
            return JSONResponse({"authenticated": False}, status_code=401)
        return JSONResponse({"authenticated": True, **s.to_ui_dict()})

    # ── Protected HTTP routes ─────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        if not _get_session(request):
            return RedirectResponse("/auth/login", status_code=302)
        html = (STATIC_DIR / "index.html")
        return HTMLResponse(html.read_text() if html.exists() else "<h1>Chatbot</h1>")

    @app.get("/health")
    async def health_live():
        """Unauthenticated liveness probe for the ALB health check."""
        return JSONResponse({"status": "ok"})

    @app.get("/api/health")
    async def health(request: Request):
        s = _get_session(request)
        if not s:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        caps = {}
        for cap in core._caps:
            ok, msg = cap.startup_check()
            caps[cap.name] = {"ok": ok, "message": msg}
        return JSONResponse({"status": "ok", "user": s.to_ui_dict(), "capabilities": caps})

    @app.get("/api/sessions")
    async def list_sessions(request: Request):
        if not _get_session(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return JSONResponse({"sessions": mongo.list_sessions(20)})

    @app.delete("/api/sessions/{chat_id}")
    async def delete_session(request: Request, chat_id: str):
        if not _get_session(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        mongo.delete_session(chat_id)
        redis.clear(chat_id)
        _chat_sessions.pop(chat_id, None)
        return JSONResponse({"deleted": chat_id})

    # ── WebSocket ─────────────────────────────────────────────────────────────

    @app.websocket("/ws/{chat_id}")
    async def ws_endpoint(websocket: WebSocket, chat_id: str):
        """
        WebSocket chat. Session cookie verified on upgrade.
        User's access_token injected into Dremio capability for delegated auth.
        """
        sid          = websocket.cookies.get(COOKIE_NAME)
        user_session = store.get_session(sid) if sid else None
        if not user_session:
            await websocket.close(code=4001, reason="Unauthorized")
            return

        await websocket.accept()

        # Inject delegated tokens into capabilities for this user
        _inject_user_token(core, user_session.access_token)
        _inject_snowflake_user(core, user_session.user_id)
        _inject_snowflake_token(core, user_session.snowflake_token,
                                user_session.snowflake_token_expires_at,
                                user_session.refresh_token)
        _inject_dremio_token(core, user_session.dremio_token,
                             user_session.dremio_token_expires_at)

        chat_session = _get_chat_session(
            user_session.chat_session_id or chat_id)
        log.info("WS: %s chat=%s", user_session.email, chat_id[:8])

        async def send(msg: dict):
            try: await websocket.send_json(msg)
            except Exception: pass

        try:
            while True:
                try:
                    raw = await asyncio.wait_for(
                        websocket.receive_text(), timeout=300)
                except asyncio.TimeoutError:
                    await send({"type": "ping"}); continue

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await send({"type": "error", "text": "Invalid JSON"}); continue

                t = msg.get("type", "question")

                if t == "ping":
                    await send({"type": "pong"}); continue

                if t == "reset":
                    new_id = str(uuid.uuid4())
                    user_session.chat_session_id = new_id
                    chat_session = _get_chat_session(new_id)
                    await send({"type": "reset", "chat_id": new_id}); continue

                if t == "question":
                    q = msg.get("text", "").strip()
                    if not q: continue
                    await send({"type": "thinking"})
                    loop   = asyncio.get_event_loop()
                    result = await asyncio.to_thread(
                        _ask_with_events, core, chat_session, q, send, loop,
                        user_session.user_id)
                    await send({"type": "answer", "text": result.answer,
                                "chat_id": chat_session.session_id})
                    await send({"type": "usage", "input": result.input_tokens,
                                "cached": result.cached_tokens,
                                "output": result.output_tokens})

        except WebSocketDisconnect:
            log.info("WS disconnected: %s", user_session.email)
        except Exception as e:
            log.error("WS error: %s", e, exc_info=True)
            try: await websocket.send_json({"type": "error", "text": str(e)})
            except Exception: pass

    return app


# ── Helpers ───────────────────────────────────────────────────────────────────

def _inject_user_token(core: Any, access_token: str) -> None:
    """Give the Dremio capability the user's delegated token for this request."""
    for cap in core._caps:
        if cap.name == "dremio" and hasattr(cap, "set_user_token"):
            cap.set_user_token(access_token)


def _inject_snowflake_user(core: Any, user_id: str) -> None:
    """Give the Snowflake capability the Entra user_id (for schema cache keying)."""
    for cap in core._caps:
        if cap.name == "snowflake" and hasattr(cap, "set_user_id"):
            cap.set_user_id(user_id)


def _inject_snowflake_token(core: Any, token: str, expires_at: float,
                             refresh_token: str = "") -> None:
    """Give the Snowflake capability the SSO token obtained at Entra login."""
    for cap in core._caps:
        if cap.name == "snowflake" and hasattr(cap, "set_snowflake_token"):
            cap.set_snowflake_token(token, expires_at, refresh_token)


def _inject_dremio_token(core: Any, token: str, expires_at: float) -> None:
    """Give the Dremio capability the OAuth token from /auth/dremio/connect."""
    for cap in core._caps:
        if cap.name == "dremio" and hasattr(cap, "set_dremio_token"):
            cap.set_dremio_token(token, expires_at)


def _ask_with_events(core, session, question, send_coro, loop, user_id: str = ""):
    orig = core._dispatch

    def _fire(msg):
        asyncio.run_coroutine_threadsafe(send_coro(msg), loop)

    def _with_events(name, inputs):
        _fire({"type": "tool_call", "tool": name,
               "inputs": {k: str(v)[:80] for k, v in inputs.items()}})
        result = orig(name, inputs)
        ok = "error" not in str(result).lower()[:50]
        _fire({"type": "tool_result", "tool": name, "ok": ok,
               "preview": str(result)[:120]})
        return result

    core._dispatch = _with_events
    try:
        return core.ask(question, session, user_id=user_id)
    finally:
        core._dispatch = orig


# ── Entry point ───────────────────────────────────────────────────────────────

def run(core, mongo, redis, cap_names: list[str],
        host: str = "127.0.0.1", port: int = 8443,
        cert_file: str = None, key_file: str = None):
    import uvicorn
    from auth.tls import ensure_certs
    cert, key = (cert_file, key_file) if cert_file else ensure_certs()
    app = create_app(core, mongo, redis, cap_names)
    log.info("HTTPS → https://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port,
                ssl_certfile=cert, ssl_keyfile=key,
                log_level="info")
