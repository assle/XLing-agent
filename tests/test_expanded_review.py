"""Tests for issue 11: Expanded human review context + audit.

Covers:
  - PrivacySanitizer (phone/email/id/name redaction)
  - build_review_summary (desensitized summary)
  - create_with_context (handoff reason, desensitized summary)
  - mark_decision (approve/reject/refer/monitor + audit fields)
  - Invalid handoff reason / decision raises
  - Already reviewed can't be re-decided
  - _to_dict includes new fields

Run: python -m pytest tests/test_expanded_review.py
"""
from __future__ import annotations

from datetime import datetime

from app.core.config import Settings
from app.core.security import hash_password
from app.models.entities import ChatMessage, ChatSession, PsychologicalReport, ReviewRequest, UserAccount
from app.services.privacy import PrivacySanitizer
from app.services.review import HANDOFF_REASONS, ReviewService
from tests.support import DatabaseHarness

_TestSession = DatabaseHarness().sessions
_settings = Settings()


def _seed():
    db = _TestSession()
    try:
        u = UserAccount(username="admin", display_name="Admin", password_hash=hash_password("admin123"))
        u.roles = {"ROLE_ADMIN", "ROLE_USER"}
        s = UserAccount(username="student", display_name="Student", password_hash=hash_password("s"))
        s.roles = {"ROLE_USER"}
        session = ChatSession(public_id="sess-1", title="test", user_id=2)
        report = PsychologicalReport(
            user_id=2, session_id=1, content="test content",
            intent="CONSULT", emotion="ANXIETY", emotion_score=2.5,
            risk_level="HIGH", confidence=0.9, summary="risk",
        )
        db.add_all([u, s, session, report])
        db.commit()
    finally:
        db.close()

_seed()


def _svc():
    return ReviewService(_TestSession(), _settings)


def _clean():
    db = _TestSession()
    try:
        db.query(ReviewRequest).delete()
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# PrivacySanitizer
# ---------------------------------------------------------------------------

def test_sanitize_phone():
    assert "[手机号已隐藏]" in PrivacySanitizer.sanitize("我电话是13812345678")

def test_sanitize_email():
    assert "[邮箱已隐藏]" in PrivacySanitizer.sanitize("邮箱test@example.com")

def test_sanitize_name():
    result = PrivacySanitizer.sanitize("我叫张三，最近很焦虑")
    assert "张三" not in result
    assert "焦虑" in result

def test_sanitize_keeps_content():
    result = PrivacySanitizer.sanitize("最近考研压力很大")
    assert "考研压力" in result

def test_build_review_summary():
    summary = PrivacySanitizer.build_review_summary(
        current_difficulty="考研冲刺阶段焦虑",
        risk_trend="rising",
        cbt_summary="触发事件：考试压力",
        action_plan_status="active",
    )
    assert "当前困境" in summary
    assert "风险轨迹趋势" in summary
    assert "认知行为四维追问" in summary
    assert "行动计划状态" in summary

def test_build_review_summary_empty():
    summary = PrivacySanitizer.build_review_summary()
    assert "无可用" in summary


# ---------------------------------------------------------------------------
# create_with_context
# ---------------------------------------------------------------------------

def test_create_with_context():
    _clean()
    svc = _svc()
    try:
        review = svc.create_with_context(
            session_id=1, report_id=1, thread_id="thread-1",
            risk_summary="high risk detected",
            handoff_reason="HIGH_RISK_KEYWORD",
            desensitized_summary="当前困境：焦虑",
        )
        assert review.handoff_reason == "HIGH_RISK_KEYWORD"
        assert review.desensitized_summary == "当前困境：焦虑"
        assert review.status == "pending"
    finally:
        svc.db.close()

def test_create_with_invalid_handoff_raises():
    _clean()
    svc = _svc()
    try:
        try:
            svc.create_with_context(1, 1, "t", handoff_reason="INVALID")
            assert False, "Should have raised"
        except ValueError:
            pass
    finally:
        svc.db.close()


# ---------------------------------------------------------------------------
# mark_decision
# ---------------------------------------------------------------------------

def test_mark_decision_approve():
    _clean()
    svc = _svc()
    try:
        review = svc.create_with_context(1, 1, "t", handoff_reason="USER_REQUEST")
        result = svc.mark_decision(review.id, "approve", "looks ok", "admin")
        assert result.reviewer_decision == "approve"
        assert result.reviewer_note == "looks ok"
        assert result.reviewed_by == "admin"
        assert result.status == "approved"
        assert result.reviewed_at is not None
    finally:
        svc.db.close()

def test_mark_decision_reject():
    _clean()
    svc = _svc()
    try:
        review = svc.create_with_context(1, 1, "t")
        result = svc.mark_decision(review.id, "reject")
        assert result.status == "rejected"
    finally:
        svc.db.close()

def test_mark_decision_refer():
    _clean()
    svc = _svc()
    try:
        review = svc.create_with_context(1, 1, "t")
        result = svc.mark_decision(
            review.id,
            "refer",
            "请尽快联系学校心理中心",
            "admin",
            referral_target="学校心理中心",
            next_step="今天联系值班老师",
        )
        assert result.status == "referred"
        assert result.referral_target == "学校心理中心"
        assert result.next_step == "今天联系值班老师"
        message = svc.student_message(result)
        assert "学校心理中心" in message
        assert "今天联系值班老师" in message
        persisted = svc.persist_student_message(result)
        assert persisted == message
        assert svc.db.query(ChatMessage).filter(ChatMessage.content == message).count() == 1
    finally:
        svc.db.close()

def test_mark_decision_monitor():
    _clean()
    svc = _svc()
    try:
        review = svc.create_with_context(1, 1, "t")
        result = svc.mark_decision(
            review.id,
            "monitor",
            follow_up_owner="辅导员李老师",
            follow_up_at=datetime(2026, 8, 27, 10, 0),
        )
        assert result.status == "monitoring"
        assert result.follow_up_owner == "辅导员李老师"
        assert result.follow_up_at == datetime(2026, 8, 27, 10, 0)
        message = svc.student_message(result)
        assert "辅导员李老师" in message
        assert "2026-08-27 10:00" in message
    finally:
        svc.db.close()


def test_refer_requires_target_and_next_step():
    _clean()
    svc = _svc()
    try:
        review = svc.create_with_context(1, 1, "t")
        try:
            svc.mark_decision(review.id, "refer")
            assert False, "referral details are required"
        except ValueError as exc:
            assert "referral_target" in str(exc)
    finally:
        svc.db.close()


def test_blank_refer_fields_are_rejected():
    _clean()
    svc = _svc()
    try:
        review = svc.create_with_context(1, 1, "t")
        try:
            svc.mark_decision(review.id, "refer", referral_target=" ", next_step="下一步")
            assert False, "blank referral target is not actionable"
        except ValueError:
            pass
    finally:
        svc.db.close()


def test_monitor_requires_owner_and_follow_up_time():
    _clean()
    svc = _svc()
    try:
        review = svc.create_with_context(1, 1, "t")
        try:
            svc.mark_decision(review.id, "monitor")
            assert False, "monitoring details are required"
        except ValueError as exc:
            assert "follow_up_owner" in str(exc)
    finally:
        svc.db.close()

def test_invalid_decision_raises():
    _clean()
    svc = _svc()
    try:
        review = svc.create_with_context(1, 1, "t")
        try:
            svc.mark_decision(review.id, "invalid")
            assert False, "Should have raised"
        except ValueError:
            pass
    finally:
        svc.db.close()

def test_cannot_re_decide():
    _clean()
    svc = _svc()
    try:
        review = svc.create_with_context(1, 1, "t")
        svc.mark_decision(review.id, "approve")
        try:
            svc.mark_decision(review.id, "reject")
            assert False, "Should have raised"
        except ValueError:
            pass
    finally:
        svc.db.close()


# ---------------------------------------------------------------------------
# _to_dict includes new fields
# ---------------------------------------------------------------------------

def test_to_dict_includes_new_fields():
    _clean()
    svc = _svc()
    try:
        svc.create_with_context(
            1, 1, "t", handoff_reason="RISK_TRAJECTORY_RISING",
            desensitized_summary="trajectory rising",
        )
        items = svc.list_pending()
        assert len(items) == 1
        assert items[0]["handoffReason"] == "RISK_TRAJECTORY_RISING"
        assert items[0]["desensitizedSummary"] == "trajectory rising"
    finally:
        svc.db.close()


# ---------------------------------------------------------------------------
# All handoff reasons are valid
# ---------------------------------------------------------------------------

def test_all_handoff_reasons_accepted():
    _clean()
    svc = _svc()
    try:
        for reason in HANDOFF_REASONS:
            review = svc.create_with_context(1, 1, f"t-{reason}", handoff_reason=reason)
            assert review.handoff_reason == reason
    finally:
        svc.db.close()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
