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
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    return session


def _seed_review(db, thread_id: str, status: str = "pending", minutes_ago: int = 0, risk_level: str = "HIGH"):
    """Insert a user, session, messages, report, and review request. Returns review_id."""
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
    db = _make_db()
    _seed_review(db, "thread-med-001", status="pending", minutes_ago=30, risk_level="MEDIUM")
    _seed_review(db, "thread-high-001", status="pending", minutes_ago=1, risk_level="HIGH")
    svc = ReviewService(db, Settings(ai_provider="mock"))
    pending = svc.list_pending()
    # HIGH risk comes first even though it waited only 1 min vs MEDIUM's 30 min
    assert pending[0]["riskLevel"] == "HIGH"
    assert pending[1]["riskLevel"] == "MEDIUM"


def test_list_pending_same_risk_longest_wait_first():
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
    db = _make_db()
    review_id = _seed_review(db, "thread-recent-001", status="pending", minutes_ago=5)
    svc = ReviewService(db, Settings(ai_provider="mock", review_timeout_minutes=15))
    escalated = svc.escalate_timed_out()
    assert escalated == []  # nothing escalated
    review = svc.get_review(review_id)
    assert review.status == "pending"  # still pending


def test_escalate_saves_fallback_message():
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
    db = _make_db()
    _seed_review(db, "thread-esc-pending", status="pending", minutes_ago=5)
    _seed_review(db, "thread-esc-timeout", status="pending", minutes_ago=20)
    svc = ReviewService(db, Settings(ai_provider="mock", review_timeout_minutes=15))
    svc.escalate_timed_out()
    pending = svc.list_pending()
    assert len(pending) == 1  # only the recent one remains
    assert pending[0]["threadId"] == "thread-esc-pending"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# resume_and_respond: approve / reject / degraded paths
# ---------------------------------------------------------------------------

def _interrupted_runtime(thread_id: str):
    """Interrupt a HIGH-risk run so the shared checkpointer holds the state."""
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
    result = asyncio.run(runtime.run(user, session, "我不想活了", "我不想活了"))
    assert result.pending_review is True


def test_resume_and_respond_approve_persists_ai_message():
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
    import asyncio

    from app.agents.langgraph_runtime import LangGraphAgentRuntimeService
    from app.services.ai import PromptTemplates

    async def unavailable(self, thread_id: str, approved: bool):
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
