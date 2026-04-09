"""capabilities/__init__.py"""
from .base import Capability
from .knowledge_graph import KnowledgeGraphCapability
from .dremio import DremioCapability
from .snowflake import SnowflakeCapability

REGISTRY: dict[str, type[Capability]] = {
    "kg":        KnowledgeGraphCapability,
    "dremio":    DremioCapability,
    "snowflake": SnowflakeCapability,
}

__all__ = ["Capability", "KnowledgeGraphCapability", "DremioCapability",
           "SnowflakeCapability", "REGISTRY"]
