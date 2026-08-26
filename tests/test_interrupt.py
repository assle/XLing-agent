"""Tests for HIGH-risk interrupt trigger (issue 04).

Verifies that:
- LangGraph runtime interrupts on HIGH risk -> pending_review=True, no response
- CHAT and LOW/MEDIUM risk do NOT interrupt
- Custom runtime (no LangGraph) does NOT interrupt -- HIGH gets direct response

Run:  python tests/test_interrupt.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import Settings
from app.core.enums import IntentType, RiskLevel
from app.models.entities import ChatSession, UserAccount
from app.schemas.dtos import AiMessage
from app.agents.runtime import AgentRuntimeService
from app.agents.langgraph_runtime import LangGraphAgentRuntimeService
from app.services.ai import AiClient, PromptTemplates
from app.services.assessment import PsychologicalAssessmentService


class FakeMemoryStore:
    def load_recent(self, session_public_id: str) -> list[AiMessage]:
        return [AiMessage(role="user", content="你好"), AiMessage(role="assistant", content="你好呀")]

    def messages_from_rows(self, rows):  # noqa: ANN001
        return []

    def replace(self, session_public_id: str, messages: list[AiMessage]) -> None:
        pass

    def save_cbt_state(self, session_public_id: str, state: dict) -> None:
        pass

    def load_cbt_state(self, session_public_id: str) -> dict:
        return {}


class FakeKnowledgeService:
    def retrieve(self, query: str, top_k: int | None = None):  # noqa: ANN001
        return []


def _setup_runtime(cls):
    runtime = cls.__new__(cls)
    runtime.db = None
    runtime.settings = Settings(ai_provider="mock", langgraph_checkpoint_backend="memory")
    runtime.ai = AiClient(runtime.settings)
    runtime.memory = FakeMemoryStore()
    runtime.knowledge = FakeKnowledgeService()
    runtime.assessment = PsychologicalAssessmentService(runtime.ai)
    if hasattr(runtime, "_build_graph"):
        runtime._sqlite_conn = None
        runtime._checkpointer = runtime._make_checkpointer()
        runtime.graph = runtime._build_graph()
    return runtime


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


# ---------------------------------------------------------------------------
# LangGraph runtime: CHAT does not interrupt
# ---------------------------------------------------------------------------

def test_langgraph_chat_no_interrupt():
    runtime = _setup_runtime(LangGraphAgentRuntimeService)
    user, session = _user_session("interrupt-chat-001")
    result = asyncio.run(runtime.run(user, session, "帮我写一段 Python 代码", "帮我写一段 Python 代码"))
    assert result.pending_review is False
    assert len(result.response_messages) > 0


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
# Custom runtime: HIGH risk does NOT interrupt (accepted limitation)
# ---------------------------------------------------------------------------

def test_custom_runtime_high_risk_no_interrupt():
    runtime = _setup_runtime(AgentRuntimeService)
    user, session = _user_session("custom-high-001")
    result = asyncio.run(runtime.run(user, session, "我不想活了", "我不想活了"))
    assert result.intent == IntentType.RISK
    assert result.risk_level == RiskLevel.HIGH
    assert result.pending_review is False  # custom runtime never interrupts
    assert len(result.response_messages) > 0  # counselor generated a response


# ---------------------------------------------------------------------------
# Crisis acknowledgment message is non-empty and not AI-generated
# ---------------------------------------------------------------------------

def test_crisis_acknowledgment_is_fixed_message():
    ack = PromptTemplates.crisis_acknowledgment()
    assert isinstance(ack, str)
    assert len(ack) > 20
    assert "辅导员" in ack or "心理" in ack  # contains crisis resource info


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


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    passed = 0
    failed = 0
    for test in _TESTS:
        try:
            test()
            print(f"  PASS  {test.__name__}")
            passed += 1
        except AssertionError as exc:
            print(f"  FAIL  {test.__name__}: {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERROR {test.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(_TESTS)} total")
    sys.exit(1 if failed else 0)
