"""capabilities/base.py — Capability abstract base class."""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any


class Capability(ABC):
    """
    Abstract base for a chatbot capability module.

    A capability contributes:
      tools()           → tool definitions sent to Claude on every call
      system_fragment() → routing instructions (~3-6 lines) in system prompt
      static_context()  → large stable text → cached system prompt block
      handle_tool()     → executes a tool call, returns JSON-serialisable dict
      startup_check()   → verify connectivity; return (ok, message)
    """
    name: str = "unnamed"
    description: str = ""

    @abstractmethod
    def tools(self) -> list[dict]: ...

    @abstractmethod
    def system_fragment(self) -> str: ...

    def static_context(self) -> str:
        """Large stable reference text (schema, catalog) for the prompt cache."""
        return ""

    @abstractmethod
    def handle_tool(self, name: str, inputs: dict) -> Any: ...

    def handles(self, tool_name: str) -> bool:
        return any(t["name"] == tool_name for t in self.tools())

    def dynamic_fragment(self, user_id: str) -> str:
        """
        Per-user context appended to the system prompt on every turn.
        Override in capabilities whose visible schema varies by user (e.g. Snowflake).
        Unlike static_context(), this is never globally cached.
        """
        return ""

    def startup_check(self) -> tuple[bool, str]:
        return True, f"{self.name} ready"
