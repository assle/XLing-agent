"""Tests for counselor resume -- approve and reject paths (issue 05).

Verifies that:
- approve -> CounselorAgent generates an AI response (response_messages non-empty)
- reject -> fixed fallback response (fallback_response set, counselor skipped)
- both clear pending_review after resume

Run:  python tests/test_resume.py
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


class FakeKnowledgeService:
    def retrieve(self, query: str, top_k: int | None = None):  # noqa: ANN001
        return []


def _make_runtime() -> LangGraphAgentRuntimeService:
    runtime = LangGraphAgentRuntimeService.__new__(LangGraphAgentRuntimeService)
    runtime.db = None
    runtime.settings = Settings(ai_provider="mock", langgraph_checkpoint_backend="memory")
    runtime.ai = AiClient(runtime.settings)
    runtime.memory = FakeMemoryStore()
    runtime.knowledge = FakeKnowledgeService()
    runtime.assessment = PsychologicalAssessmentService(runtime.ai)
    runtime._sqlite_conn = None
    runtime._checkpointer = runtime._make_checkpointer()
    runtime.graph = runtime._build_graph()
    return runtime


def _user_session(public_id: str):
    user = UserAccount(id=1, display_name="测试学生", roles_csv="ROLE_USER")
    session = ChatSession(id=1, public_id=public_id, user_id=1)
    return user, session


def _interrupt_high_risk(runtime, thread_id):
    """Run a HIGH-risk input to trigger interrupt, then return."""
    user, session = _user_session(thread_id)
    result = asyncio.run(runtime.run(user, session, "我不想活了", "我不想活了"))
    assert result.pending_review is True


# ---------------------------------------------------------------------------
# Approve: counselor generates AI response
# ---------------------------------------------------------------------------

def test_resume_approve_generates_response():
    runtime = _make_runtime()
    _interrupt_high_risk(runtime, "resume-approve-001")
    result = asyncio.run(runtime.resume("resume-approve-001", approved=True))
    assert result.pending_review is False
    assert result.fallback_response is None
    assert len(result.response_messages) > 0  # counselor generated response
    assert any("Counselor" in step.agent for step in result.steps)  # counselor ran


# ---------------------------------------------------------------------------
# Reject: fixed fallback, counselor skipped
# ---------------------------------------------------------------------------

def test_resume_reject_sends_fallback():
    runtime = _make_runtime()
    _interrupt_high_risk(runtime, "resume-reject-001")
    result = asyncio.run(runtime.resume("resume-reject-001", approved=False))
    assert result.pending_review is False
    assert result.fallback_response is not None
    assert result.fallback_response == PromptTemplates.fallback_response()
    assert len(result.response_messages) == 0  # no AI response
    assert not any("Counselor" in step.agent for step in result.steps)  # counselor skipped


# ---------------------------------------------------------------------------
# Fallback is a fixed safety message (not AI-generated)
# ---------------------------------------------------------------------------

def test_fallback_is_fixed_safety_message():
    runtime = _make_runtime()
    _interrupt_high_risk(runtime, "resume-fallback-001")
    result = asyncio.run(runtime.resume("resume-fallback-001", approved=False))
    fallback = result.fallback_response
    assert "辅导员" in fallback or "心理" in fallback  # crisis resources
    assert "400-161-9995" in fallback  # hotline number


# ---------------------------------------------------------------------------
# Approve response is AI-generated (differs from fallback)
# ---------------------------------------------------------------------------

def test_approve_response_is_not_fallback():
    runtime = _make_runtime()
    _interrupt_high_risk(runtime, "resume-notfallback-001")
    result = asyncio.run(runtime.resume("resume-notfallback-001", approved=True))
    # The approve response is AI-generated, not the fixed fallback
    assert result.fallback_response is None
    assert len(result.response_messages) > 0
    # response_messages content should not be the fallback text
    all_content = " ".join(m.content for m in result.response_messages)
    assert PromptTemplates.fallback_response() not in all_content


# ---------------------------------------------------------------------------
# Cross-runtime resume: a NEW runtime instance (e.g. API endpoint) can resume
# an interrupted run from a different runtime instance (shared MemorySaver)
# ---------------------------------------------------------------------------

def test_cross_runtime_approve():
    runtime_a = _make_runtime()
    _interrupt_high_risk(runtime_a, "cross-rt-approve-001")
    # Discard runtime_a, create a fresh runtime_b (simulates API endpoint)
    runtime_b = _make_runtime()
    result = asyncio.run(runtime_b.resume("cross-rt-approve-001", approved=True))
    assert result.pending_review is False
    assert len(result.response_messages) > 0  # counselor generated response


def test_cross_runtime_reject():
    runtime_a = _make_runtime()
    _interrupt_high_risk(runtime_a, "cross-rt-reject-001")
    runtime_b = _make_runtime()
    result = asyncio.run(runtime_b.resume("cross-rt-reject-001", approved=False))
    assert result.fallback_response is not None
    assert len(result.response_messages) == 0


# ---------------------------------------------------------------------------
# Issue 08: resume auto-degrades when checkpoint is lost (e.g. after restart)
# ---------------------------------------------------------------------------

def test_resume_degrades_on_missing_checkpoint():
    # Resume on a thread that was never run = no checkpoint (simulates restart)
    runtime = _make_runtime()
    result = asyncio.run(runtime.resume("never-run-degrade-001", approved=True))
    assert result.degraded is True
    assert result.fallback_response is not None
    assert result.pending_review is False
    assert len(result.response_messages) == 0  # no AI response, just fallback


def test_resume_degrade_same_for_approve_and_reject():
    # Whether counselor clicks approve or reject, missing checkpoint -> same fallback
    runtime = _make_runtime()
    result_approve = asyncio.run(runtime.resume("never-run-degrade-approve", approved=True))
    result_reject = asyncio.run(runtime.resume("never-run-degrade-reject", approved=False))
    assert result_approve.degraded is True
    assert result_reject.degraded is True
    assert result_approve.fallback_response == result_reject.fallback_response


def test_resume_normal_still_works_after_degrade_logic():
    # Normal resume (checkpoint exists) must still work after adding degrade check
    runtime = _make_runtime()
    _interrupt_high_risk(runtime, "degrade-normal-001")
    result = asyncio.run(runtime.resume("degrade-normal-001", approved=True))
    assert result.degraded is False
    assert len(result.response_messages) > 0  # counselor generated response


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
