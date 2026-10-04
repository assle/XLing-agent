from __future__ import annotations

from importlib.util import find_spec

from sqlalchemy.orm import Session

from app.agents.langgraph_runtime import LangGraphAgentRuntimeService
from app.agents.runtime import AgentRuntimeDependencies
from app.core.config import Settings


def create_agent_runtime(
    db: Session, settings: Settings, dependencies: AgentRuntimeDependencies | None = None,
) -> LangGraphAgentRuntimeService:
    """Create the only supported orchestrator; missing framework dependencies fail explicitly."""
    if not langgraph_available():
        raise RuntimeError("LangGraph is required for conversation execution")
    return LangGraphAgentRuntimeService(db, settings, dependencies)


def agent_framework_status() -> dict:
    available = langgraph_available()
    return {
        "active": "langgraph" if available else "unavailable",
        "langgraphAvailable": available,
    }


def langgraph_available() -> bool:
    return find_spec("langgraph") is not None
