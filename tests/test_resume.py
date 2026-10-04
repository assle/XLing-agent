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
    """创建使用模拟模型和已有短期历史的图执行器。

    默认共享进程内状态保存器，供跨实例恢复测试使用。
    """
    return build_runtime(
        LangGraphAgentRuntimeService,
        memory=FakeMemoryStore([
            AiMessage(role="user", content="你好"),
            AiMessage(role="assistant", content="你好呀"),
        ]),
    )


def _user_session(public_id: str):
    """构造指定公开编号的测试会话及普通用户。

    编号用于关联暂停与恢复操作。
    """
    user = UserAccount(id=1, display_name="测试学生", roles_csv="ROLE_USER")
    session = ChatSession(id=1, public_id=public_id, user_id=1)
    return user, session


def _interrupt_high_risk(runtime, thread_id):
    """发送明确高风险表达并确认已进入待审核。

    确保后续恢复测试从真实暂停状态开始。
    """
    user, session = _user_session(thread_id)
    result = asyncio.run(runtime.run(user, session, "我不想活了"))
    assert result.pending_review is True


# ---------------------------------------------------------------------------
# Approve: counselor generates AI response
# ---------------------------------------------------------------------------

def test_resume_approve_generates_response():
    """先暂停高风险会话再提交批准。

    检查退出待审核、未使用固定备用回复，并执行支持回复规划。
    """
    runtime = _make_runtime()
    _interrupt_high_risk(runtime, "resume-approve-001")
    result = asyncio.run(runtime.resume("resume-approve-001", approved=True))
    assert result.pending_review is False
    assert result.fallback_response is None
    assert len(result.response_messages) > 0  # 已准备模型回复消息，不代表此处已生成最终正文。
    assert any("Counselor" in step.agent for step in result.steps)  # 支持回复规划步骤已执行。


# ---------------------------------------------------------------------------
# Reject: fixed fallback, counselor skipped
# ---------------------------------------------------------------------------

def test_resume_reject_sends_fallback():
    """对暂停会话提交拒绝。

    检查返回固定安全回复、模型输入为空且没有执行支持回复规划。
    """
    runtime = _make_runtime()
    _interrupt_high_risk(runtime, "resume-reject-001")
    result = asyncio.run(runtime.resume("resume-reject-001", approved=False))
    assert result.pending_review is False
    assert result.fallback_response is not None
    assert result.fallback_response == PromptTemplates.fallback_response()
    assert len(result.response_messages) == 0  # 没有交给模型生成的回复消息。
    assert not any("Counselor" in step.agent for step in result.steps)  # 未执行支持回复规划。


# ---------------------------------------------------------------------------
# Fallback is a fixed safety message (not AI-generated)
# ---------------------------------------------------------------------------

def test_fallback_is_fixed_safety_message():
    """拒绝高风险会话后检查返回文字。

    确认包含专业支持与当地紧急服务方向。
    """
    runtime = _make_runtime()
    _interrupt_high_risk(runtime, "resume-fallback-001")
    result = asyncio.run(runtime.resume("resume-fallback-001", approved=False))
    fallback = result.fallback_response
    assert "专业支持" in fallback
    assert "当地紧急服务" in fallback


# ---------------------------------------------------------------------------
# Approve response is AI-generated (differs from fallback)
# ---------------------------------------------------------------------------

def test_approve_response_is_not_fallback():
    """批准暂停会话后检查模型回复输入。

    确认没有把拒绝时的固定文字混入正常批准路径。
    """
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
    """在一个实例中暂停，在同进程的另一实例中批准。

    检查共享状态支持跨实例继续规划回复，不代表跨进程恢复。
    """
    runtime_a = _make_runtime()
    _interrupt_high_risk(runtime_a, "cross-rt-approve-001")
    # Discard runtime_a, create a fresh runtime_b (simulates API endpoint)
    runtime_b = _make_runtime()
    result = asyncio.run(runtime_b.resume("cross-rt-approve-001", approved=True))
    assert result.pending_review is False
    assert len(result.response_messages) > 0  # 已准备模型回复消息，不代表此处已生成最终正文。


def test_cross_runtime_reject():
    """在另一执行器实例中拒绝已有暂停会话。

    检查仍能取得固定安全回复且不生成模型输入。
    """
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
    """尝试批准从未保存过状态的会话编号。

    检查明确标记降级并返回固定回复，不声称恢复成功。
    """
    runtime = _make_runtime()
    result = asyncio.run(runtime.resume("never-run-degrade-001", approved=True))
    assert result.degraded is True
    assert result.fallback_response is not None
    assert result.pending_review is False
    assert len(result.response_messages) == 0  # 只返回固定安全回复，不准备模型生成消息。


def test_resume_degrade_same_for_approve_and_reject():
    # Whether counselor clicks approve or reject, missing checkpoint -> same fallback
    """对两个缺失状态编号分别批准和拒绝。

    检查都进入降级且采用相同安全回复。
    """
    runtime = _make_runtime()
    result_approve = asyncio.run(runtime.resume("never-run-degrade-approve", approved=True))
    result_reject = asyncio.run(runtime.resume("never-run-degrade-reject", approved=False))
    assert result_approve.degraded is True
    assert result_reject.degraded is True
    assert result_approve.fallback_response == result_reject.fallback_response


def test_resume_normal_still_works_after_degrade_logic():
    # Normal resume (checkpoint exists) must still work after adding degrade check
    """从真实暂停状态正常批准。

    检查降级逻辑不会把有效恢复误判为失败。
    """
    runtime = _make_runtime()
    _interrupt_high_risk(runtime, "degrade-normal-001")
    result = asyncio.run(runtime.resume("degrade-normal-001", approved=True))
    assert result.degraded is False
    assert len(result.response_messages) > 0  # 已准备模型回复消息，不代表此处已生成最终正文。
