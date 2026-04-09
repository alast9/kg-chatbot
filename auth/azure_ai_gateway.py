"""
auth/azure_ai_gateway.py — Azure AI Foundry Agent Service MCP gateway client
=============================================================================
Calls the Azure AI Foundry MCP gateway using JSON-RPC 2.0 over HTTPS.
Auth is an Entra ID Bearer token (client_credentials for M2M, or user
delegated token for 3LO Snowflake/Dremio calls).

The gateway exposes MCP tools via the MCP 2025-11-25 protocol:
  POST /mcp  {"jsonrpc":"2.0","method":"tools/list","id":"..."}
  POST /mcp  {"jsonrpc":"2.0","method":"tools/call","id":"...","params":{...}}

Azure AI Foundry Agent Service supports MCP version 2025-11-25 with full
three-legged OAuth (3LO) so user identity flows through to the data layer.

Gateway URL is configured via AZURE_AI_FOUNDRY_GATEWAY_URL env var.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

from .sso import EntraTokenManager, get_token_manager

log = logging.getLogger("auth.azure_gateway")

GATEWAY_URL = os.getenv(
    "AZURE_AI_FOUNDRY_GATEWAY_URL",
    ""
)


class AzureAIGatewayClient:
    """
    JSON-RPC 2.0 client for an Azure AI Foundry Agent Service MCP gateway.
    Uses Auth0 client_credentials tokens for M2M authentication (3LO-compatible).

    Handles:
      - Token fetch + automatic refresh on expiry
      - JSON-RPC request/response envelope (MCP 2025-11-25)
      - Error extraction from JSON-RPC error objects
      - Retries on 401 (token expired mid-request)
    """

    def __init__(self,
                 gateway_url:   str = GATEWAY_URL,
                 token_manager: EntraTokenManager | None = None):
        self._url   = gateway_url
        self._auth  = token_manager or get_token_manager()
        self._tools_cache: list[dict] | None = None

    # ── JSON-RPC helpers ──────────────────────────────────────────────────────

    def _rpc(self, method: str, params: dict | None = None,
             retry_on_401: bool = True) -> Any:
        """
        Execute a JSON-RPC 2.0 request.
        Returns the 'result' field on success.
        Raises RuntimeError on JSON-RPC error or HTTP error.
        """
        req_id  = str(uuid.uuid4())
        body    = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            body["params"] = params

        token   = self._auth.get_token()
        data    = json.dumps(body).encode()
        request = urllib.request.Request(
            self._url, data=data, method="POST",
            headers={"Content-Type":  "application/json",
                     "Authorization": f"Bearer {token}"})

        try:
            with urllib.request.urlopen(request, timeout=60) as r:
                resp = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 401 and retry_on_401:
                log.info("Gateway returned 401 — forcing token refresh")
                self._auth._invalidate()
                return self._rpc(method, params, retry_on_401=False)
            body_err = e.read().decode()[:400]
            raise RuntimeError(f"Gateway HTTP {e.code}: {body_err}")
        except Exception as e:
            raise RuntimeError(f"Gateway request failed: {e}")

        if "error" in resp:
            err = resp["error"]
            raise RuntimeError(f"Gateway RPC error {err.get('code')}: {err.get('message')}")

        return resp.get("result")

    def _rpc_with_token(self, method: str, params: dict | None = None,
                        token: str = "") -> Any:
        """Like _rpc() but uses an explicit token (user's delegated token for 3LO)."""
        req_id  = str(uuid.uuid4())
        body    = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            body["params"] = params
        data    = json.dumps(body).encode()
        request = urllib.request.Request(
            self._url, data=data, method="POST",
            headers={"Content-Type":  "application/json",
                     "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(request, timeout=60) as r:
                resp = json.loads(r.read())
        except urllib.error.HTTPError as e:
            body_err = e.read().decode()[:400]
            raise RuntimeError(f"Gateway HTTP {e.code}: {body_err}")
        except Exception as e:
            raise RuntimeError(f"Gateway request failed: {e}")
        if "error" in resp:
            err = resp["error"]
            raise RuntimeError(f"Gateway RPC error {err.get('code')}: {err.get('message')}")
        return resp.get("result")

    # ── MCP protocol methods ──────────────────────────────────────────────────

    def list_tools(self, use_cache: bool = True) -> list[dict]:
        """Fetch the list of tools exposed by the gateway."""
        if use_cache and self._tools_cache is not None:
            return self._tools_cache

        result = self._rpc("tools/list")
        tools  = result.get("tools", []) if isinstance(result, dict) else []
        self._tools_cache = tools
        log.info("Azure AI Foundry gateway tools/list: %d tools", len(tools))
        return tools

    def call_tool(self, name: str, arguments: dict | None = None) -> Any:
        """Call a tool using the M2M token from Auth0."""
        params = {"name": name, "arguments": arguments or {}}
        result = self._rpc("tools/call", params)
        return self._extract_content(result)

    def call_tool_with_token(self, name: str, arguments: dict | None = None,
                              token: str = "") -> Any:
        """
        Call a tool using a user's delegated Bearer token (3LO flow).
        Used for Dremio calls where user identity must reach the data layer.
        """
        params = {"name": name, "arguments": arguments or {}}
        result = self._rpc_with_token("tools/call", params, token)
        return self._extract_content(result)

    def _extract_content(self, result: Any) -> Any:
        """Extract actual content from MCP tool result envelope."""
        if result is None:
            return {}
        if isinstance(result, dict) and "content" in result:
            content = result["content"]
            if isinstance(content, list):
                texts = [c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("type") == "text"]
                combined = "\n".join(texts)
                try:
                    return json.loads(combined)
                except Exception:
                    return {"text": combined}
        return result

    def startup_check(self) -> tuple[bool, str]:
        """Verify gateway connectivity and list tools."""
        try:
            tools = self.list_tools(use_cache=False)
            names = [t.get("name", "?") for t in tools]
            return True, f"Azure AI Foundry gateway OK — {len(tools)} tools: {names}"
        except Exception as e:
            return False, f"Azure AI Foundry gateway FAILED: {e}"


# Module-level singleton
_default_client: AzureAIGatewayClient | None = None


def get_gateway_client() -> AzureAIGatewayClient:
    global _default_client
    if _default_client is None:
        _default_client = AzureAIGatewayClient()
    return _default_client
