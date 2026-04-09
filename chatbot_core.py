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
import re
from dataclasses import dataclass, field
from typing import Any

import openai

from capabilities.base import Capability
from session import SessionManager

log = logging.getLogger("core")

AZURE_AI_ENDPOINT = os.getenv("AZURE_AI_ENDPOINT", "")
AZURE_AI_API_KEY  = os.getenv("AZURE_AI_API_KEY",  "")
AZURE_AI_MODEL    = os.getenv("AZURE_AI_MODEL",    "DeepSeek-R1")

_llm_client = openai.OpenAI(
    base_url=AZURE_AI_ENDPOINT or None,
    api_key=AZURE_AI_API_KEY or "placeholder",
    default_query={"api-version": "2024-05-01-preview"},
)

GENERIC_INTRO = """\
You are a versatile AI assistant with multiple analytical capabilities.
Route questions to the appropriate capability based on what the user is asking.
Be concise and accurate. Cite data when you have it.
If a question spans capabilities (e.g. cross-referencing Dremio customer data with
the internal knowledge graph), use tools from both.
"""

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _strip_think(text: str) -> str:
    """Remove DeepSeek-R1 chain-of-thought blocks from the final answer."""
    return _THINK_RE.sub("", text).strip()


def _to_openai_tools(anthropic_tools: list[dict]) -> list[dict]:
    """Convert Anthropic tool format → OpenAI function calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        for t in anthropic_tools
    ]


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
        self._sys_cache: str | None = None   # built once, reused

        for cap in capabilities:
            self._tools.extend(cap.tools())

        log.info("ChatbotCore ready — %d capabilities, %d tools: %s",
                 len(capabilities), len(self._tools),
                 [c.name for c in capabilities])

    # ── System prompt (built once, cached) ───────────────────────────────────

    def _build_system(self, session_id: str, turn: int, user_id: str = "") -> str:
        if self._sys_cache is None:
            parts = [GENERIC_INTRO]
            for cap in self._caps:
                frag = cap.system_fragment()
                if frag:
                    parts.append(f"\n── {cap.name.upper()} ──\n{frag}")
                ctx = cap.static_context()
                if ctx:
                    parts.append(f"\n{ctx}")
            self._sys_cache = "\n".join(parts)
            log.info("System prompt built: ~%d tokens", len(self._sys_cache) // 4)

        dynamic = "\n".join(
            cap.dynamic_fragment(user_id)
            for cap in self._caps
            if cap.dynamic_fragment(user_id)
        ) if user_id else ""

        suffix = f"\n\nSession: {session_id} | Turn: {turn}"
        return self._sys_cache + (f"\n\n{dynamic}" if dynamic else "") + suffix

    # ── LLM call ─────────────────────────────────────────────────────────────

    def _llm(self, messages: list, system: str) -> dict:
        oai_messages = [{"role": "system", "content": system}] + messages
        kwargs: dict = {
            "model":      AZURE_AI_MODEL,
            "max_tokens": 2048,
            "messages":   oai_messages,
        }
        if self._tools:
            kwargs["tools"] = _to_openai_tools(self._tools)
        resp = _llm_client.chat.completions.create(**kwargs)
        return resp.model_dump()

    # ── Tool dispatch ─────────────────────────────────────────────────────────

    def _dispatch(self, name: str, inputs: dict) -> Any:
        for cap in self._caps:
            if cap.handles(name):
                result = cap.handle_tool(name, inputs)
                if result is not None:
                    return result
        return {"error": f"No capability handles tool: {name}"}

    # ── Core ask — the only public method ─────────────────────────────────────

    def ask(self, question: str, session: SessionManager, user_id: str = "") -> TurnResult:
        """
        Execute one turn. Thread-safe — all state is local.

        1. Record user message in session
        2. Agentic loop: LLM → tool calls → results → LLM → ...
        3. Record assistant answer in session
        4. Return TurnResult(answer, tool_events, token_usage)
        """
        session.add_user(question)

        messages     = session.history          # includes the question just added
        system       = self._build_system(session.session_id, session.turn_count, user_id)
        tool_events: list[ToolEvent] = []
        answer       = ""
        usage        = {}

        while True:
            try:
                resp = self._llm(messages, system)
            except openai.APIStatusError as e:
                log.error("LLM HTTP %d: %s", e.status_code, e.message)
                answer = f"⚠️ LLM error {e.status_code}: {e.message}"
                break
            except Exception as e:
                log.error("LLM call failed: %s", e)
                answer = f"⚠️ Error communicating with LLM: {e}"
                break

            usage   = resp.get("usage", {})
            choice  = resp["choices"][0]
            message = choice["message"]
            finish  = choice.get("finish_reason")

            log.info("Tokens — input: %d | output: %d",
                     usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))

            if finish == "tool_calls":
                tool_calls   = message.get("tool_calls") or []
                tool_results = []
                for tc in tool_calls:
                    fn    = tc["function"]
                    name  = fn["name"]
                    try:
                        inputs = json.loads(fn.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        inputs = {}
                    event = ToolEvent(tool_name=name, inputs=inputs)
                    try:
                        result = self._dispatch(name, inputs)
                        event.result = result
                    except Exception as e:
                        result = {"error": str(e)}
                        event.error = str(e)
                    tool_events.append(event)
                    log.debug("Tool %s → %s", name, str(result)[:120])
                    tool_results.append({
                        "role":         "tool",
                        "tool_call_id": tc["id"],
                        "content":      json.dumps(result, default=str),
                    })

                messages = list(messages)   # local copy to extend
                messages.append({
                    "role":       "assistant",
                    "content":    message.get("content"),
                    "tool_calls": tool_calls,
                })
                messages.extend(tool_results)

            else:
                answer = _strip_think(message.get("content") or "")
                break

        session.add_assistant(answer)

        return TurnResult(
            answer        = answer,
            tool_events   = tool_events,
            cached_tokens = 0,
            input_tokens  = usage.get("prompt_tokens", 0),
            output_tokens = usage.get("completion_tokens", 0),
        )
