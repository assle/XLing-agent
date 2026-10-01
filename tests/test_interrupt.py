"""Tests for HIGH-risk interrupt trigger (issue 04).

Verifies that:
- LangGraph runtime interrupts on HIGH risk -> pending_review=True, no response
- CHAT and LOW/MEDIUM risk do NOT interrupt

Run: python -m pytest tests/test_interrupt.py
"""
from __future__ import annotations

import asyncio

from app.agents.langgraph_runtime import LangGraphAgentRuntimeService
from app.core.enums import IntentType, RiskLevel
from app.models.entities import ChatSession, UserAccount
from app.schemas.dtos import AiMessage
from app.services.ai import PromptTemplates
from tests.support import FakeMemoryStore, build_runtime


class FailingKnowledgeService:
    def retrieve(self, query: str, top_k: int | None = None):  # noqa: ANN001
        """一旦检索被调用就抛出断言错误。

        高风险测试用它证明审核前不会进入知识检索。
        """
        raise AssertionError("high-risk messages must not reach knowledge retrieval")


class TrackingAssessment:
    def __init__(self, order):
        """保存外层的步骤顺序列表。

        后续评估会向同一列表追加标记。
        """
        self.order = order

    async def aassess(self, text, history):  # noqa: ANN001
        """记录风险评估发生并返回固定低风险结果。

        供测试比较它与知识检索的实际调用顺序。
        """
        from app.core.enums import EmotionLabel
        from app.services.assessment import PsychologyAssessment
        self.order.append("risk")
        return PsychologyAssessment(EmotionLabel.ANXIETY, 2.0, RiskLevel.LOW, 0.8, "stress")


class TrackingKnowledgeService:
    def __init__(self, order):
        """保存与评估替身共享的顺序列表。

        使两种服务的调用先后可直接断言。
        """
        self.order = order

    def retrieve(self, query: str, top_k: int | None = None):  # noqa: ANN001
        """记录知识检索发生并返回空结果。

        无需真实索引即可检查流程顺序。
        """
        self.order.append("knowledge")
        return []


def _setup_runtime(cls):
    """为指定执行器提供两条模拟历史和通用测试依赖。

    避免测试受到外部模型和缓存状态影响。
    """
    return build_runtime(
        cls,
        memory=FakeMemoryStore([
            AiMessage(role="user", content="你好"),
            AiMessage(role="assistant", content="你好呀"),
        ]),
    )


def _user_session(public_id: str):
    """构造带指定公开编号的用户和会话对象。

    不同编号用于隔离各例保存的执行状态。
    """
    user = UserAccount(id=1, display_name="测试学生", roles_csv="ROLE_USER")
    session = ChatSession(id=1, public_id=public_id, user_id=1)
    return user, session


# ---------------------------------------------------------------------------
# LangGraph runtime: HIGH risk triggers interrupt
# ---------------------------------------------------------------------------

def test_langgraph_high_risk_interrupts():
    """通过图执行器处理明确高风险表达。

    检查快速安全步骤位于最前，评估保存且进入待审核，普通回复列表为空。
    """
    runtime = _setup_runtime(LangGraphAgentRuntimeService)
    user, session = _user_session("interrupt-high-001")
    result = asyncio.run(runtime.run(user, session, "我不想活了", "我不想活了"))
    assert result.intent == IntentType.RISK
    assert result.risk_level == RiskLevel.HIGH
    assert result.pending_review is True
    assert len(result.response_messages) == 0  # 审核前尚未准备普通支持回复。
    assert result.assessment is not None
    assert result.assessment.risk == RiskLevel.HIGH
    assert result.quick_safety_checked is True
    assert result.quick_risk_flagged is True
    assert result.steps[0].action == "QUICK_SAFETY_CHECK"


def test_langgraph_high_risk_skips_knowledge_retrieval():
    """替换成一经调用就失败的知识服务。

    高风险运行应正常进入待审核，证明没有调用检索。
    """
    runtime = _setup_runtime(LangGraphAgentRuntimeService)
    runtime.knowledge = FailingKnowledgeService()
    user, session = _user_session("interrupt-before-knowledge-001")
    result = asyncio.run(runtime.run(user, session, "我不想活了", "我不想活了"))
    assert result.pending_review is True


def test_langgraph_assesses_support_before_knowledge():
    """用共享列表记录低风险支持消息的实际步骤。

    检查风险评估发生在知识检索之前且无需等待审核。
    """
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
    """运行普通编程问题。

    检查完成快速安全检查但未标记高风险，直接准备回复。
    """
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
    """运行普通焦虑表达。

    检查已有回复规划且不进入人工审核暂停。
    """
    runtime = _setup_runtime(LangGraphAgentRuntimeService)
    user, session = _user_session("interrupt-consult-001")
    result = asyncio.run(runtime.run(user, session, "最近压力很大，很焦虑", "最近压力很大，很焦虑"))
    assert result.pending_review is False
    assert len(result.response_messages) > 0


# ---------------------------------------------------------------------------
# Crisis acknowledgment message is non-empty and not AI-generated
# ---------------------------------------------------------------------------

def test_crisis_acknowledgment_is_fixed_message():
    """读取等待审核时的固定确认语。

    检查非空且包含专业支持方向，不依赖模型生成。
    """
    ack = PromptTemplates.crisis_acknowledgment()
    assert isinstance(ack, str)
    assert len(ack) > 20
    assert "专业支持" in ack


# ---------------------------------------------------------------------------
# 暂停时等待的是审核关口节点，收到决定后才能继续。
# ---------------------------------------------------------------------------

def test_interrupted_state_has_gate_pending():
    """高风险暂停后读取保存状态。

    检查等待继续的节点是审核关口，便于之后恢复。
    """
    runtime = _setup_runtime(LangGraphAgentRuntimeService)
    user, session = _user_session("interrupt-next-001")
    asyncio.run(runtime.run(user, session, "我不想活了", "我不想活了"))
    state = runtime.get_state("interrupt-next-001")
    assert state.next  # non-empty -> interrupted
    assert "risk_guardian_gate" in state.next  # gate node paused, awaiting resume
