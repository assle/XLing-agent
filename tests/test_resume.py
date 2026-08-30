"""Tests for counselor resume -- approve and reject paths (issue 05).

Verifies that:
- approve -> CounselorAgent generates an AI response (response_messages non-empty)
- reject -> fixed fallback response (fallback_response set, counselor skipped)
- both clear pending_review after resume

Run: python -m pytest tests/test_resume.py
"""
from __future__ import annotations

import asyncio

from app.agents.langgraph_runtime import LangGraphAgentRuntimeService
from app.models.entities import ChatSession, UserAccount
from app.schemas.dtos import AiMessage
from app.services.ai import PromptTemplates
from tests.support import FakeMemoryStore, build_runtime


def _make_runtime() -> LangGraphAgentRuntimeService:
    return build_runtime(
        LangGraphAgentRuntimeService,
        memory=FakeMemoryStore([
            AiMessage(role="user", content="你好"),
            AiMessage(role="assistant", content="你好呀"),
        ]),
    )


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
