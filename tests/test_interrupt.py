"""Tests for HIGH-risk interrupt trigger (issue 04).

Verifies that:
- LangGraph runtime interrupts on HIGH risk -> pending_review=True, no response
- CHAT and LOW/MEDIUM risk do NOT interrupt
- Custom runtime (no LangGraph) also returns pending_review for HIGH risk

Run: python -m pytest tests/test_interrupt.py
"""
from __future__ import annotations

import asyncio

from app.agents.langgraph_runtime import LangGraphAgentRuntimeService
from app.agents.runtime import AgentRuntimeService
from app.core.enums import IntentType, RiskLevel
from app.models.entities import ChatSession, UserAccount
from app.schemas.dtos import AiMessage
from app.services.ai import PromptTemplates
from tests.support import FakeMemoryStore, build_runtime


class FailingKnowledgeService:
    def retrieve(self, query: str, top_k: int | None = None):  # noqa: ANN001
        raise AssertionError("high-risk messages must not reach knowledge retrieval")


class TrackingAssessment:
    def __init__(self, order):
        self.order = order

    async def aassess(self, text, history):  # noqa: ANN001
        from app.core.enums import EmotionLabel
        from app.services.assessment import PsychologyAssessment
        self.order.append("risk")
        return PsychologyAssessment(EmotionLabel.ANXIETY, 2.0, RiskLevel.LOW, 0.8, "stress")


class TrackingKnowledgeService:
    def __init__(self, order):
        self.order = order

    def retrieve(self, query: str, top_k: int | None = None):  # noqa: ANN001
        self.order.append("knowledge")
        return []


def _setup_runtime(cls):
    return build_runtime(
        cls,
        memory=FakeMemoryStore([
            AiMessage(role="user", content="你好"),
            AiMessage(role="assistant", content="你好呀"),
        ]),
    )


def _user_session(public_id: str):
    user = UserAccount(id=1, display_name="测试学生", roles_csv="ROLE_USER")
    session = ChatSession(id=1, public_id=public_id, user_id=1)
    return user, session


# ---------------------------------------------------------------------------
# LangGraph runtime: HIGH risk triggers interrupt
# ---------------------------------------------------------------------------

def test_langgraph_high_risk_interrupts():
    runtime = _setup_runtime(LangGraphAgentRuntimeService)
    user, session = _user_session("interrupt-high-001")
    result = asyncio.run(runtime.run(user, session, "我不想活了", "我不想活了"))
    assert result.intent == IntentType.RISK
    assert result.risk_level == RiskLevel.HIGH
    assert result.pending_review is True
    assert len(result.response_messages) == 0  # counselor did not run
    assert result.assessment is not None
    assert result.assessment.risk == RiskLevel.HIGH
    assert result.quick_safety_checked is True
    assert result.quick_risk_flagged is True
    assert result.steps[0].action == "QUICK_SAFETY_CHECK"


def test_langgraph_high_risk_skips_knowledge_retrieval():
    runtime = _setup_runtime(LangGraphAgentRuntimeService)
    runtime.knowledge = FailingKnowledgeService()
    user, session = _user_session("interrupt-before-knowledge-001")
    result = asyncio.run(runtime.run(user, session, "我不想活了", "我不想活了"))
    assert result.pending_review is True


def test_langgraph_assesses_support_before_knowledge():
    runtime = _setup_runtime(LangGraphAgentRuntimeService)
    order = []
    runtime.assessment = TrackingAssessment(order)
    runtime.knowledge = TrackingKnowledgeService(order)
    user, session = _user_session("risk-before-knowledge-001")
    result = asyncio.run(runtime.run(user, session, "最近压力很大", "最近压力很大"))
    assert result.pending_review is False
    assert order[:2] == ["risk", "knowledge"]


# ---------------------------------------------------------------------------
# LangGraph runtime: CHAT does not interrupt
# ---------------------------------------------------------------------------

def test_langgraph_chat_no_interrupt():
    runtime = _setup_runtime(LangGraphAgentRuntimeService)
    user, session = _user_session("interrupt-chat-001")
    result = asyncio.run(runtime.run(user, session, "帮我写一段 Python 代码", "帮我写一段 Python 代码"))
    assert result.pending_review is False
    assert len(result.response_messages) > 0
    assert result.quick_safety_checked is True
    assert result.quick_risk_flagged is False


# ---------------------------------------------------------------------------
# LangGraph runtime: CONSULT with LOW risk does not interrupt
# ---------------------------------------------------------------------------

def test_langgraph_consult_low_no_interrupt():
    runtime = _setup_runtime(LangGraphAgentRuntimeService)
    user, session = _user_session("interrupt-consult-001")
    result = asyncio.run(runtime.run(user, session, "最近压力很大，很焦虑", "最近压力很大，很焦虑"))
    assert result.pending_review is False
    assert len(result.response_messages) > 0


# ---------------------------------------------------------------------------
# Custom runtime: HIGH risk also enters human review
# ---------------------------------------------------------------------------

def test_custom_runtime_high_risk_enters_review():
    runtime = _setup_runtime(AgentRuntimeService)
    user, session = _user_session("custom-high-001")
    result = asyncio.run(runtime.run(user, session, "我不想活了", "我不想活了"))
    assert result.intent == IntentType.RISK
    assert result.risk_level == RiskLevel.HIGH
    assert result.pending_review is True
    assert result.response_messages == []


def test_custom_runtime_high_risk_skips_knowledge_retrieval():
    runtime = _setup_runtime(AgentRuntimeService)
    runtime.knowledge = FailingKnowledgeService()
    user, session = _user_session("custom-before-knowledge-001")
    result = asyncio.run(runtime.run(user, session, "我不想活了", "我不想活了"))
    assert result.risk_level == RiskLevel.HIGH
    assert result.pending_review is True
    assert result.response_messages == []


# ---------------------------------------------------------------------------
# Crisis acknowledgment message is non-empty and not AI-generated
# ---------------------------------------------------------------------------

def test_crisis_acknowledgment_is_fixed_message():
    ack = PromptTemplates.crisis_acknowledgment()
    assert isinstance(ack, str)
    assert len(ack) > 20
    assert "专业支持" in ack


# ---------------------------------------------------------------------------
# Interrupted state has counselor as next node (can be resumed)
# ---------------------------------------------------------------------------

def test_interrupted_state_has_gate_pending():
    runtime = _setup_runtime(LangGraphAgentRuntimeService)
    user, session = _user_session("interrupt-next-001")
    asyncio.run(runtime.run(user, session, "我不想活了", "我不想活了"))
    state = runtime.get_state("interrupt-next-001")
    assert state.next  # non-empty -> interrupted
    assert "risk_guardian_gate" in state.next  # gate node paused, awaiting resume
