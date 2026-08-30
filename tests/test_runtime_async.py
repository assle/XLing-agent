"""Integration tests for async agent runtime (issue 02: async end-to-end).

Verifies that async run() produces correct results for both the custom
runtime and the LangGraph runtime (ainvoke), across CHAT and support paths.

Uses mock AiClient + fake memory/knowledge stores -- no external services.

Run: python -m pytest tests/test_runtime_async.py
"""
from __future__ import annotations

import asyncio

from app.agents.langgraph_runtime import LangGraphAgentRuntimeService
from app.agents.runtime import AgentRuntimeService
from app.core.enums import IntentType, RiskLevel
from app.models.entities import ChatSession, UserAccount
from app.schemas.dtos import AiMessage
from tests.support import FakeMemoryStore, build_runtime


def _setup_runtime(cls):
    """Create a runtime through its dependency seam."""
    return build_runtime(
        cls,
        memory=FakeMemoryStore([
            AiMessage(role="user", content="你好"),
            AiMessage(role="assistant", content="你好呀，我在。"),
        ]),
    )


def _user_session():
    user = UserAccount(id=1, display_name="测试学生", roles_csv="ROLE_USER")
    session = ChatSession(id=1, public_id="test-session-001", user_id=1)
    return user, session


# ---------------------------------------------------------------------------
# Custom runtime: CHAT path (keyword shortcut, no LLM for classify)
# ---------------------------------------------------------------------------

def test_custom_runtime_chat():
    runtime = _setup_runtime(AgentRuntimeService)
    user, session = _user_session()
    result = asyncio.run(runtime.run(user, session, "帮我写一段 Python 代码", "帮我写一段 Python 代码"))
    assert result.intent == IntentType.CHAT
    assert result.risk_level == RiskLevel.LOW
    assert len(result.response_messages) > 0
    # CHAT path skips knowledge and risk
    assert result.retrieved_knowledge == []
    assert result.assessment is None


# ---------------------------------------------------------------------------
# LangGraph runtime: CHAT path via ainvoke
# ---------------------------------------------------------------------------

def test_langgraph_runtime_chat():
    runtime = _setup_runtime(LangGraphAgentRuntimeService)
    user, session = _user_session()
    result = asyncio.run(runtime.run(user, session, "帮我写一段 Python 代码", "帮我写一段 Python 代码"))
    assert result.intent == IntentType.CHAT
    assert len(result.response_messages) > 0


# ---------------------------------------------------------------------------
# Custom runtime: CONSULT support path (LLM classify + knowledge + risk)
# ---------------------------------------------------------------------------

def test_custom_runtime_consult():
    runtime = _setup_runtime(AgentRuntimeService)
    user, session = _user_session()
    result = asyncio.run(runtime.run(user, session, "最近压力很大，很焦虑", "最近压力很大，很焦虑"))
    assert result.intent in (IntentType.CONSULT, IntentType.RISK)
    assert result.assessment is not None  # risk was assessed
    assert len(result.response_messages) > 0  # counselor response planned


# ---------------------------------------------------------------------------
# LangGraph runtime: CONSULT support path via ainvoke
# ---------------------------------------------------------------------------

def test_langgraph_runtime_consult():
    runtime = _setup_runtime(LangGraphAgentRuntimeService)
    user, session = _user_session()
    result = asyncio.run(runtime.run(user, session, "最近压力很大，很焦虑", "最近压力很大，很焦虑"))
    assert result.intent in (IntentType.CONSULT, IntentType.RISK)
    assert result.assessment is not None
    assert len(result.response_messages) > 0


# ---------------------------------------------------------------------------
# RISK path: high-risk keyword triggers RISK intent
# ---------------------------------------------------------------------------

def test_custom_runtime_risk_keyword():
    runtime = _setup_runtime(AgentRuntimeService)
    user, session = _user_session()
    result = asyncio.run(runtime.run(user, session, "我不想活了", "我不想活了"))
    assert result.intent == IntentType.RISK
    assert result.risk_level == RiskLevel.HIGH
    assert result.assessment is not None


def test_langgraph_runtime_risk_keyword():
    runtime = _setup_runtime(LangGraphAgentRuntimeService)
    user, session = _user_session()
    result = asyncio.run(runtime.run(user, session, "我不想活了", "我不想活了"))
    assert result.intent == IntentType.RISK
    assert result.risk_level == RiskLevel.HIGH


# ---------------------------------------------------------------------------
# Parity: custom and LangGraph produce same intent for same input
# ---------------------------------------------------------------------------

def test_custom_and_langgraph_parity_chat():
    custom = _setup_runtime(AgentRuntimeService)
    langgraph = _setup_runtime(LangGraphAgentRuntimeService)
    user, session = _user_session()
    text = "帮我写一段 Python 代码"
    custom_result = asyncio.run(custom.run(user, session, text, text))
    langgraph_result = asyncio.run(langgraph.run(user, session, text, text))
    assert custom_result.intent == langgraph_result.intent
