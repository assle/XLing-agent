"""Tests for ReviewService listing and status updates (issue 06).

Uses an in-memory SQLite database to test the review queue logic without
MySQL/Redis. The resume/AI-streaming integration is tested in test_resume.py.

Run: python -m pytest tests/test_review.py
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.database import Base
from app.core.enums import MessageRole
from app.core.time import utc_now
from app.models.entities import ChatMessage, ChatSession, ReviewRequest, SafetyAssessmentRecord, UserAccount
from app.services.review import ReviewService, ReviewTimeoutWorker
from tests.support import FakeMemoryStore, build_runtime


def _make_db():
    """创建当前测试独享的内存数据库并建立表结构。

    返回可直接准备审核数据的会话，不连接默认数据库。
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    return session


def _seed_review(db, thread_id: str, status: str = "pending", minutes_ago: int = 0, risk_level: str = "HIGH"):
    """按指定风险、状态和等待分钟数保存完整审核样本。

    先建立用户、会话及消息，再创建评估和审核，返回审核编号。
    """
    user = UserAccount(username=f"u_{thread_id}", display_name="测试学生", password_hash="x", roles_csv="ROLE_USER")
    db.add(user)
    db.flush()
    session = ChatSession(public_id=thread_id, user_id=user.id, title="test")
    db.add(session)
    db.flush()
    db.add(ChatMessage(user_id=user.id, session_id=session.id, role=MessageRole.USER.value, content="我不想活了"))
    db.add(ChatMessage(user_id=user.id, session_id=session.id, role=MessageRole.ASSISTANT.value, content="我听到了你"))
    db.flush()
    report = SafetyAssessmentRecord(
        user_id=user.id, session_id=session.id, content="我不想活了",
        intent="RISK", emotion="HIGH_RISK", emotion_score=4.0,
        risk_level=risk_level, confidence=0.95, summary="检测到明确高风险表达",
    )
    db.add(report)
    db.flush()
    review = ReviewRequest(
        session_id=session.id, report_id=report.id, thread_id=thread_id,
        risk_summary="检测到明确高风险表达", status=status,
        created_at=utc_now() - timedelta(minutes=minutes_ago),
    )
    db.add(review)
    db.commit()
    return review.id


# ---------------------------------------------------------------------------
# list_pending returns only pending reviews
# ---------------------------------------------------------------------------

def test_list_pending_returns_only_pending():
    """准备待审核和已批准混合记录。

    检查列表只包含待处理项且保留风险摘要。
    """
    db = _make_db()
    _seed_review(db, "thread-001", status="pending")
    _seed_review(db, "thread-002", status="approved")
    _seed_review(db, "thread-003", status="pending")
    svc = ReviewService(db, Settings(ai_provider="mock"))
    pending = svc.list_pending()
    assert len(pending) == 2
    assert all(item["riskSummary"] for item in pending)


# ---------------------------------------------------------------------------
# list_pending includes context fields
# ---------------------------------------------------------------------------

def test_list_pending_includes_context():
    """准备一条包含两条历史消息的审核。

    检查列表带会话编号、评估字段、上下文和非负等待时间。
    """
    db = _make_db()
    _seed_review(db, "thread-ctx-001", status="pending")
    svc = ReviewService(db, Settings(ai_provider="mock"))
    pending = svc.list_pending()
    assert len(pending) == 1
    item = pending[0]
    assert item["threadId"] == "thread-ctx-001"
    assert item["emotion"] == "HIGH_RISK"
    assert item["studentMessage"] == "我不想活了"
    assert len(item["recentContext"]) == 2  # user + assistant messages
    assert item["waitSeconds"] is not None
    assert item["waitSeconds"] >= 0


# ---------------------------------------------------------------------------
# mark_decision(approve / reject) updates status
# ---------------------------------------------------------------------------

def test_mark_approved_removes_from_pending():
    """把待审核记录批准。

    检查它离开待处理列表，并保存批准状态和处理时间。
    """
    db = _make_db()
    review_id = _seed_review(db, "thread-approve-001", status="pending")
    svc = ReviewService(db, Settings(ai_provider="mock"))
    svc.mark_decision(review_id, "approve")
    pending = svc.list_pending()
    assert len(pending) == 0  # no longer pending
    review = svc.get_review(review_id)
    assert review.status == "approved"
    assert review.reviewed_at is not None


def test_mark_rejected_removes_from_pending():
    """把待审核记录拒绝。

    检查列表不再显示待处理，记录状态和时间已更新。
    """
    db = _make_db()
    review_id = _seed_review(db, "thread-reject-001", status="pending")
    svc = ReviewService(db, Settings(ai_provider="mock"))
    svc.mark_decision(review_id, "reject")
    pending = svc.list_pending()
    assert len(pending) == 0
    review = svc.get_review(review_id)
    assert review.status == "rejected"
    assert review.reviewed_at is not None


def test_mark_escalated_removes_from_pending():
    """将待审核记录标为安全升级。

    检查状态变化并离开待处理队列。
    """
    db = _make_db()
    review_id = _seed_review(db, "thread-escalated-001", status="pending")
    svc = ReviewService(db, Settings(ai_provider="mock"))
    svc.mark_escalated(review_id)
    pending = svc.list_pending()
    assert len(pending) == 0
    review = svc.get_review(review_id)
    assert review.status == "escalated"
    assert review.reviewed_at is not None


# ---------------------------------------------------------------------------
# mark on non-pending review raises
# ---------------------------------------------------------------------------

def test_mark_non_pending_raises():
    """先批准一条审核，再尝试重复批准。

    要求抛错，避免已处理记录重复决定。
    """
    db = _make_db()
    review_id = _seed_review(db, "thread-dup-001", status="pending")
    svc = ReviewService(db, Settings(ai_provider="mock"))
    svc.mark_decision(review_id, "approve")
    # Second decision should raise (already approved)
    try:
        svc.mark_decision(review_id, "approve")
        assert False, "should have raised"
    except ValueError:
        pass  # expected


# ---------------------------------------------------------------------------
# list_pending ordered oldest first
# ---------------------------------------------------------------------------

def test_list_pending_oldest_first():
    """建立风险相同但等待时间不同的审核。

    检查等待更久的排在前面。
    """
    db = _make_db()
    _seed_review(db, "thread-new-001", status="pending", minutes_ago=1)
    _seed_review(db, "thread-old-001", status="pending", minutes_ago=30)
    svc = ReviewService(db, Settings(ai_provider="mock"))
    pending = svc.list_pending()
    assert pending[0]["threadId"] == "thread-old-001"  # oldest first
    assert pending[1]["threadId"] == "thread-new-001"


# ---------------------------------------------------------------------------
# Issue 07: urgency ordering -- higher risk first, then longer wait
# ---------------------------------------------------------------------------

def test_list_pending_high_before_medium():
    """建立等待较短的高风险和等待较长的中风险。

    检查风险优先级高于等待时长。
    """
    db = _make_db()
    _seed_review(db, "thread-med-001", status="pending", minutes_ago=30, risk_level="MEDIUM")
    _seed_review(db, "thread-high-001", status="pending", minutes_ago=1, risk_level="HIGH")
    svc = ReviewService(db, Settings(ai_provider="mock"))
    pending = svc.list_pending()
    # HIGH risk comes first even though it waited only 1 min vs MEDIUM's 30 min
    assert pending[0]["riskLevel"] == "HIGH"
    assert pending[1]["riskLevel"] == "MEDIUM"


def test_list_pending_same_risk_longest_wait_first():
    """让两项都为高风险，只改变等待时间。

    检查同级风险按等待更久优先。
    """
    db = _make_db()
    _seed_review(db, "thread-wait-short", status="pending", minutes_ago=1, risk_level="HIGH")
    _seed_review(db, "thread-wait-long", status="pending", minutes_ago=30, risk_level="HIGH")
    svc = ReviewService(db, Settings(ai_provider="mock"))
    pending = svc.list_pending()
    assert pending[0]["threadId"] == "thread-wait-long"  # longer wait first
    assert pending[1]["threadId"] == "thread-wait-short"


# ---------------------------------------------------------------------------
# Issue 07: timeout escalation (15 min, action = auto-fallback)
# ---------------------------------------------------------------------------

def test_escalate_timed_out_marks_escalated():
    """准备超过十五分钟门槛的审核。

    检查升级编号、状态、系统处理者和超时说明均保存。
    """
    db = _make_db()
    review_id = _seed_review(db, "thread-timeout-001", status="pending", minutes_ago=20)
    svc = ReviewService(db, Settings(ai_provider="mock", review_timeout_minutes=15))
    escalated = svc.escalate_timed_out()
    assert review_id in escalated
    review = svc.get_review(review_id)
    assert review.status == "escalated"
    assert review.reviewed_at is not None
    assert review.reviewer_decision == "timeout"
    assert review.reviewed_by == "system"
    assert "超时" in review.reviewer_note


def test_timeout_worker_runs_without_admin_list_request():
    """直接运行一轮后台超时处理，不调用管理列表。

    检查超时审核被独立处理。
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    review_id = _seed_review(db, "thread-worker-timeout-001", status="pending", minutes_ago=20)
    settings = Settings(ai_provider="mock", review_timeout_minutes=15)
    worker = ReviewTimeoutWorker(settings, session_factory=factory)

    assert worker.run_once() == [review_id]
    review = ReviewService(db, settings).get_review(review_id)
    assert review.status == "escalated"


def test_timeout_worker_is_idempotent():
    """连续运行两次超时处理。

    检查仅首轮返回升级编号，固定安全回复也只保存一次。
    """
    from app.services.ai import PromptTemplates

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    review_id = _seed_review(db, "thread-worker-idempotent-001", status="pending", minutes_ago=20)
    settings = Settings(ai_provider="mock", review_timeout_minutes=15)
    worker = ReviewTimeoutWorker(settings, session_factory=factory)

    assert worker.run_once() == [review_id]
    assert worker.run_once() == []
    review = ReviewService(db, settings).get_review(review_id)
    messages = db.query(ChatMessage).filter(ChatMessage.session_id == review.session_id).all()
    assert sum(m.content == PromptTemplates.fallback_response() for m in messages) == 1


def test_escalate_skips_recent_reviews():
    """准备仍在等待期限内的审核。

    检查本轮不升级，记录继续待处理。
    """
    db = _make_db()
    review_id = _seed_review(db, "thread-recent-001", status="pending", minutes_ago=5)
    svc = ReviewService(db, Settings(ai_provider="mock", review_timeout_minutes=15))
    escalated = svc.escalate_timed_out()
    assert escalated == []  # nothing escalated
    review = svc.get_review(review_id)
    assert review.status == "pending"  # still pending


def test_escalate_saves_fallback_message():
    """让一条审核超时后查询会话消息。

    检查固定安全回复以助手角色实际保存。
    """
    from app.services.ai import PromptTemplates

    db = _make_db()
    review_id = _seed_review(db, "thread-fallback-001", status="pending", minutes_ago=20)
    svc = ReviewService(db, Settings(ai_provider="mock", review_timeout_minutes=15))
    svc.escalate_timed_out()
    # A ChatMessage with the fallback text was saved for the student
    review = svc.get_review(review_id)
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == review.session_id)
        .all()
    )
    fallback = PromptTemplates.fallback_response()
    assert any(m.content == fallback and m.role == MessageRole.ASSISTANT.value for m in messages)


def test_escalated_removed_from_pending():
    """同时准备近期和超时审核再运行超时处理。

    检查待审核列表只保留近期记录。
    """
    db = _make_db()
    _seed_review(db, "thread-esc-pending", status="pending", minutes_ago=5)
    _seed_review(db, "thread-esc-timeout", status="pending", minutes_ago=20)
    svc = ReviewService(db, Settings(ai_provider="mock", review_timeout_minutes=15))
    svc.escalate_timed_out()
    pending = svc.list_pending()
    assert len(pending) == 1  # only the recent one remains
    assert pending[0]["threadId"] == "thread-esc-pending"


# ---------------------------------------------------------------------------
# 本组测试结束。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# resume_and_respond: approve / reject / degraded paths
# ---------------------------------------------------------------------------

def _interrupted_runtime(thread_id: str):
    """为给定会话编号建立真实的高风险暂停状态。

    使用模拟模型和内存保存器，并断言待审核状态确实产生。
    """
    import asyncio

    from app.agents.langgraph_runtime import LangGraphAgentRuntimeService
    from app.schemas.dtos import AiMessage
    runtime = build_runtime(
        LangGraphAgentRuntimeService,
        settings=Settings(
            ai_provider="mock",
            langgraph_checkpoint_backend="memory",
            knowledge_vector_enabled=False,
        ),
        memory=FakeMemoryStore([AiMessage(role="user", content="你好")]),
    )

    user = UserAccount(id=1, display_name="测试学生", roles_csv="ROLE_USER")
    session = ChatSession(id=1, public_id=thread_id, user_id=1)
    result = asyncio.run(runtime.run(user, session, "我不想活了"))
    assert result.pending_review is True


def test_resume_and_respond_approve_persists_ai_message():
    """准备审核和对应暂停状态，再批准恢复。

    检查正常生成非空文字，并实际存为助手消息。
    """
    import asyncio

    db = _make_db()
    review_id = _seed_review(db, "svc-resume-approve-001")
    _interrupted_runtime("svc-resume-approve-001")
    svc = ReviewService(db, Settings(
        ai_provider="mock",
        knowledge_vector_enabled=False,
        langgraph_checkpoint_backend="memory",
    ))
    review = svc.get_review(review_id)
    response_text, degraded = asyncio.run(svc.resume_and_respond(review, approved=True))
    assert degraded is False
    assert len(response_text) > 0
    messages = db.query(ChatMessage).filter(ChatMessage.role == MessageRole.ASSISTANT.value).all()
    assert messages[-1].content == response_text


def test_resume_and_respond_reject_persists_fallback():
    """对有效暂停状态执行拒绝。

    检查未降级但保存固定安全回复，符合正常拒绝路径。
    """
    import asyncio

    from app.services.ai import PromptTemplates

    db = _make_db()
    review_id = _seed_review(db, "svc-resume-reject-001")
    _interrupted_runtime("svc-resume-reject-001")
    svc = ReviewService(db, Settings(
        ai_provider="mock",
        knowledge_vector_enabled=False,
        langgraph_checkpoint_backend="memory",
    ))
    review = svc.get_review(review_id)
    response_text, degraded = asyncio.run(svc.resume_and_respond(review, approved=False))
    assert degraded is False
    assert response_text == PromptTemplates.fallback_response()
    messages = db.query(ChatMessage).filter(ChatMessage.role == MessageRole.ASSISTANT.value).all()
    assert messages[-1].content == PromptTemplates.fallback_response()


def test_resume_and_respond_degraded_when_checkpoint_lost():
    """有审核记录但没有对应可恢复状态。

    检查批准请求转为降级并返回固定回复。
    """
    import asyncio

    from app.services.ai import PromptTemplates

    db = _make_db()
    review_id = _seed_review(db, "svc-resume-lost-001")  # never ran -> no checkpoint
    svc = ReviewService(db, Settings(ai_provider="mock", knowledge_vector_enabled=False))
    review = svc.get_review(review_id)
    response_text, degraded = asyncio.run(svc.resume_and_respond(review, approved=True))
    assert degraded is True
    assert response_text == PromptTemplates.fallback_response()


def test_resume_and_respond_degrades_when_checkpoint_store_is_unavailable(monkeypatch):
    """替换恢复方法让其抛出存储不可用异常。

    检查审核服务采用固定安全回复并标记降级。
    """
    import asyncio

    from app.agents.langgraph_runtime import LangGraphAgentRuntimeService
    from app.services.ai import PromptTemplates

    async def unavailable(self, thread_id: str, approved: bool):
        """模拟状态存储读取失败。

        通过可等待接口抛错，测试审核服务的异常保护。
        """
        raise OSError("checkpoint store unavailable")

    monkeypatch.setattr(LangGraphAgentRuntimeService, "resume", unavailable)
    db = _make_db()
    review_id = _seed_review(db, "svc-resume-unavailable-001")
    svc = ReviewService(
        db,
        Settings(
            ai_provider="mock",
            knowledge_vector_enabled=False,
            langgraph_checkpoint_backend="memory",
        ),
    )

    response_text, degraded = asyncio.run(
        svc.resume_and_respond(svc.get_review(review_id), approved=True)
    )

    assert degraded is True
    assert response_text == PromptTemplates.fallback_response()


def test_resume_and_respond_degrades_when_checkpoint_database_is_corrupt(tmp_path):
    """把非数据库字节写入临时状态文件再尝试恢复。

    检查损坏文件不会导致普通模型回复，而是返回降级安全文本。
    """
    import asyncio

    from app.services.ai import PromptTemplates

    checkpoint_path = tmp_path / "corrupt-checkpoint.db"
    checkpoint_path.write_bytes(b"not a sqlite database")
    db = _make_db()
    review_id = _seed_review(db, "svc-resume-corrupt-001")
    svc = ReviewService(
        db,
        Settings(
            ai_provider="mock",
            knowledge_vector_enabled=False,
            langgraph_checkpoint_backend="async_sqlite",
            langgraph_checkpoint_path=str(checkpoint_path),
        ),
    )

    response_text, degraded = asyncio.run(
        svc.resume_and_respond(svc.get_review(review_id), approved=False)
    )

    assert degraded is True
    assert response_text == PromptTemplates.fallback_response()
