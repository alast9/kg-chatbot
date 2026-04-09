"""
chatbot_core.py — Stateless turn executor
==========================================
ChatbotCore is a singleton shared across all sessions and interfaces.
It is STATELESS per turn — history is passed in, never stored on self.

    answer, tool_events, usage = core.ask(question, session)

The caller (CLI or Web interface) owns the session and history.
"""
from __future__ import annotations
import json
import logging
import os
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any

from capabilities.base import Capability
from session import SessionManager

log = logging.getLogger("core")

BEDROCK_TOKEN = os.getenv("AWS_BEARER_TOKEN_BEDROCK",
    "ABSKQmVkcm9ja0FQSUtleS1wY3VrLWF0LTc2MTMzNDYyNzU3NjpnL090bGE1VkZPMHg1cTNhb0g4aU1CSUVsMFpYcmlQelMwWnYwK3U4NCtXM1BWdE80emVoZnBxUTR6UT0=")
AWS_REGION    = os.getenv("AWS_REGION",    "us-east-1")
BEDROCK_MODEL = os.getenv("BEDROCK_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
BEDROCK_URL   = (f"https://bedrock-runtime.{AWS_REGION}.amazonaws.com"
                 f"/model/{BEDROCK_MODEL}/invoke")

GENERIC_INTRO = """\
You are a versatile AI assistant with multiple analytical capabilities.
Route questions to the appropriate capability based on what the user is asking.
Be concise and accurate. Cite data when you have it.
If a question spans capabilities (e.g. cross-referencing Dremio customer data with
the internal knowledge graph), use tools from both.
"""


@dataclass
class ToolEvent:
    """Emitted for each tool call — streamed to the UI for live feedback."""
    tool_name:  str
    inputs:     dict
    result:     Any = None
    error:      str = ""


@dataclass
class TurnResult:
    """The complete result of one chatbot turn."""
    answer:      str
    tool_events: list[ToolEvent] = field(default_factory=list)
    cached_tokens: int = 0
    input_tokens:  int = 0
    output_tokens: int = 0


class ChatbotCore:
    """
    Pure logic singleton. Thread-safe: all per-turn state lives in local variables.
    Shared state (capabilities list, tools list, system prompt) is read-only after init.
    """

    def __init__(self, capabilities: list[Capability]):
        self._caps      = capabilities
        self._tools     = []
        self._sys_cache: list[dict] | None = None   # built once, reused

        for cap in capabilities:
            self._tools.extend(cap.tools())

        log.info("ChatbotCore ready — %d capabilities, %d tools: %s",
                 len(capabilities), len(self._tools),
                 [c.name for c in capabilities])

    # ── System prompt (built once, cached) ───────────────────────────────────

    def _build_system(self, session_id: str, turn: int) -> list[dict]:
        """
        Two-block system:
          Block 1 (cache_control: ephemeral) — static: intro + all capability contexts
          Block 2 (no cache) — dynamic: session ID + turn
        """
        if self._sys_cache is None:
            parts = [GENERIC_INTRO]
            for cap in self._caps:
                frag = cap.system_fragment()
                if frag:
                    parts.append(f"\n── {cap.name.upper()} ──\n{frag}")
                ctx = cap.static_context()
                if ctx:
                    parts.append(f"\n{ctx}")
            cached_text  = "\n".join(parts)
            self._sys_cache = cached_text
            tokens = len(cached_text) // 4
            log.info("System prompt built: ~%d tokens", tokens)

        return [
            {"type": "text", "text": self._sys_cache,
             "cache_control": {"type": "ephemeral"}},
            {"type": "text",
             "text": f"\nSession: {session_id} | Turn: {turn}"},
        ]

    # ── Bedrock ───────────────────────────────────────────────────────────────

    def _bedrock(self, messages: list, system: list) -> dict:
        payload = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 2048,
            "system":   system,
            "tools":    self._tools,
            "messages": messages,
        }).encode()
        req = urllib.request.Request(
            BEDROCK_URL, data=payload, method="POST",
            headers={"Content-Type":   "application/json",
                     "Authorization":  f"Bearer {BEDROCK_TOKEN}",
                     "anthropic-beta": "prompt-caching-2024-07-31"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())

    # ── Tool dispatch ─────────────────────────────────────────────────────────

    def _dispatch(self, name: str, inputs: dict) -> Any:
        for cap in self._caps:
            if cap.handles(name):
                result = cap.handle_tool(name, inputs)
                if result is not None:
                    return result
        return {"error": f"No capability handles tool: {name}"}

    # ── Core ask — the only public method ─────────────────────────────────────

    def ask(self, question: str, session: SessionManager) -> TurnResult:
        """
        Execute one turn. Thread-safe — all state is local.

        1. Record user message in session
        2. Agentic loop: Bedrock → tool calls → results → Bedrock → ...
        3. Record assistant answer in session
        4. Return TurnResult(answer, tool_events, token_usage)
        """
        session.add_user(question)

        messages     = session.history          # includes the question just added
        system       = self._build_system(session.session_id, session.turn_count)
        tool_events: list[ToolEvent] = []
        answer       = ""
        usage        = {}

        while True:
            try:
                resp = self._bedrock(messages, system)
            except urllib.error.HTTPError as e:
                err = e.read().decode()[:300]
                log.error("Bedrock HTTP %d: %s", e.code, err)
                answer = f"⚠️ Bedrock error {e.code}: {err}"
                break
            except Exception as e:
                log.error("Bedrock call failed: %s", e)
                answer = f"⚠️ Error communicating with Bedrock: {e}"
                break

            usage   = resp.get("usage", {})
            stop    = resp.get("stop_reason")
            content = resp.get("content", [])

            cached  = usage.get("cache_read_input_tokens", 0)
            log.info("Tokens — input: %d (cached: %d ↓) | output: %d",
                     usage.get("input_tokens", 0), cached, usage.get("output_tokens", 0))

            if stop == "tool_use":
                tool_results = []
                for block in content:
                    if block.get("type") == "tool_use":
                        event = ToolEvent(tool_name=block["name"],
                                          inputs=block.get("input", {}))
                        try:
                            result = self._dispatch(block["name"], block.get("input", {}))
                            event.result = result
                        except Exception as e:
                            result = {"error": str(e)}
                            event.error = str(e)
                        tool_events.append(event)
                        log.debug("Tool %s → %s", block["name"], str(result)[:120])
                        tool_results.append({
                            "type":        "tool_result",
                            "tool_use_id": block["id"],
                            "content":     json.dumps(result, default=str),
                        })
                messages = list(messages)   # local copy to extend
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user",      "content": tool_results})

            else:
                answer = "".join(b.get("text", "") for b in content
                                 if b.get("type") == "text")
                break

        session.add_assistant(answer)

        return TurnResult(
            answer        = answer,
            tool_events   = tool_events,
            cached_tokens = usage.get("cache_read_input_tokens", 0),
            input_tokens  = usage.get("input_tokens", 0),
            output_tokens = usage.get("output_tokens", 0),
        )
