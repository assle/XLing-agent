"""人工支持列表与非终结超时升级的服务契约。"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.database import Base
from app.core.enums import MessageRole, ToolJobKind
from app.core.time import utc_now
from app.models.entities import (
    ChatMessage,
    ChatSession,
    ReviewRequest,
    SafetyAssessmentRecord,
    SupportAuditEvent,
    ToolJob,
    UserAccount,
)
from app.services.review import ReviewService, ReviewTimeoutWorker


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed_review(db, thread_id: str, status: str = "pending", minutes_ago: int = 0, risk_level: str = "HIGH"):
    user = UserAccount(username=f"u_{thread_id}", display_name="测试用户", password_hash="x", roles_csv="ROLE_USER")
    db.add(user)
    db.flush()
    session = ChatSession(public_id=thread_id, user_id=user.id, title="test")
    db.add(session)
    db.flush()
    db.add_all([
        ChatMessage(user_id=user.id, session_id=session.id, role=MessageRole.USER.value, content="我不想活了"),
        ChatMessage(user_id=user.id, session_id=session.id, role=MessageRole.ASSISTANT.value, content="我听到了你"),
    ])
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


def test_active_list_contains_pending_and_in_progress_but_not_terminal():
    db = _make_db()
    _seed_review(db, "thread-001", status="pending")
    _seed_review(db, "thread-002", status="in_progress")
    _seed_review(db, "thread-003", status="completed")
    items = ReviewService(db, Settings(ai_provider="mock")).list_pending()
    assert {item["status"] for item in items} == {"pending", "in_progress"}
    assert len(items) == 2
    assert all(item["riskSummary"] for item in items)


def test_active_list_includes_context():
    db = _make_db()
    _seed_review(db, "thread-ctx-001", status="pending")
    item = ReviewService(db, Settings(ai_provider="mock")).list_pending()[0]
    assert item["threadId"] == "thread-ctx-001"
    assert item["emotion"] == "HIGH_RISK"
    assert item["studentMessage"] == "我不想活了"
    assert len(item["recentContext"]) == 2
    assert item["waitSeconds"] >= 0


def test_legacy_approve_and_reject_are_invalid():
    db = _make_db()
    review_id = _seed_review(db, "thread-legacy-decision")
    service = ReviewService(db, Settings(ai_provider="mock"))
    for decision in ("approve", "reject"):
        with pytest.raises(ValueError, match="Invalid decision"):
            service.mark_decision(review_id, decision)
    assert service.get_review(review_id).status == "pending"


def test_active_list_orders_by_risk_then_wait():
    db = _make_db()
    _seed_review(db, "thread-med-old", minutes_ago=30, risk_level="MEDIUM")
    _seed_review(db, "thread-high-new", minutes_ago=1)
    _seed_review(db, "thread-high-old", minutes_ago=20)
    items = ReviewService(db, Settings(ai_provider="mock")).list_pending()
    assert [item["threadId"] for item in items] == ["thread-high-old", "thread-high-new", "thread-med-old"]


def test_timeout_adds_non_terminal_audit_and_one_alert_job():
    db = _make_db()
    review_id = _seed_review(db, "thread-timeout-001", status="in_progress", minutes_ago=20)
    review = db.get(ReviewRequest, review_id)
    review.claimed_by = "owner"
    db.commit()
    service = ReviewService(db, Settings(ai_provider="mock", review_timeout_minutes=15))

    assert service.escalate_timed_out() == [review_id]
    assert service.escalate_timed_out() == []
    db.refresh(review)
    assert review.status == "in_progress"
    assert review.claimed_by == "owner"
    assert review.reviewed_at is None
    assert review.escalation_count == 1
    assert db.query(SupportAuditEvent).filter_by(review_id=review_id, event_type="escalated").count() == 1
    assert db.query(ToolJob).filter_by(report_id=review.report_id, kind=ToolJobKind.ESCALATION_ALERT.value).count() == 1
    assert db.query(ChatMessage).filter_by(session_id=review.session_id).count() == 2


def test_timeout_worker_runs_without_admin_list_request():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    review_id = _seed_review(db, "thread-worker-timeout-001", status="pending", minutes_ago=20)
    settings = Settings(ai_provider="mock", review_timeout_minutes=15)
    worker = ReviewTimeoutWorker(settings, session_factory=factory)

    assert worker.run_once() == [review_id]
    assert worker.run_once() == []
    review = ReviewService(db, settings).get_review(review_id)
    assert review.status == "pending"
    assert review.escalation_count == 1


def test_timeout_skips_recent_reviews():
    db = _make_db()
    review_id = _seed_review(db, "thread-recent-001", status="pending", minutes_ago=5)
    service = ReviewService(db, Settings(ai_provider="mock", review_timeout_minutes=15))
    assert service.escalate_timed_out() == []
    review = service.get_review(review_id)
    assert review.status == "pending"
    assert review.escalation_count == 0
