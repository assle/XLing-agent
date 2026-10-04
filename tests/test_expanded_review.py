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
from app.models.entities import ChatMessage, ChatSession, ReviewRequest, SafetyAssessmentRecord, UserAccount
from app.services.privacy import PrivacySanitizer
from app.services.review import HANDOFF_REASONS, ReviewService
from tests.support import DatabaseHarness

_TestSession = DatabaseHarness().sessions
_settings = Settings()


def _seed():
    """准备管理员、普通用户、会话及高风险评估记录。

    作为后续审核字段和决定测试的共同基础。
    """
    db = _TestSession()
    try:
        u = UserAccount(username="admin", display_name="Admin", password_hash=hash_password("admin123"))
        u.roles = {"ROLE_ADMIN", "ROLE_USER"}
        s = UserAccount(username="student", display_name="Student", password_hash=hash_password("s"))
        s.roles = {"ROLE_USER"}
        session = ChatSession(public_id="sess-1", title="test", user_id=2)
        report = SafetyAssessmentRecord(
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
    """为本次测试新建数据库会话并包装审核服务。

    调用方负责关闭服务持有的会话。
    """
    return ReviewService(_TestSession(), _settings)


def _clean():
    """清空审核请求记录并提交。

    保留共同基础数据，便于每例重新创建审核。
    """
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
    """输入含匹配手机号的文本。

    检查该格式的号码被替换为隐藏标记。
    """
    assert "[手机号已隐藏]" in PrivacySanitizer.sanitize("我电话是13812345678")

def test_sanitize_email():
    """输入含邮箱的文本。

    检查模式匹配将邮箱替换成隐藏标记。
    """
    assert "[邮箱已隐藏]" in PrivacySanitizer.sanitize("邮箱test@example.com")

def test_sanitize_name():
    """输入带姓名提示和焦虑内容的句子。

    检查姓名被去除，同时保留困扰内容。
    """
    result = PrivacySanitizer.sanitize("我叫张三，最近很焦虑")
    assert "张三" not in result
    assert "焦虑" in result

def test_sanitize_keeps_content():
    """输入没有匹配身份信息的支持表达。

    检查普通困扰内容未被误删。
    """
    result = PrivacySanitizer.sanitize("最近考研压力很大")
    assert "考研压力" in result

def test_build_review_summary():
    """提供困境、趋势、四维摘要和计划状态。

    检查输出包含四种中文分段标签。
    """
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
    """不给任何摘要信息。

    检查返回明确的无可用内容说明。
    """
    summary = PrivacySanitizer.build_review_summary()
    assert "无可用" in summary


# ---------------------------------------------------------------------------
# create_with_context
# ---------------------------------------------------------------------------

def test_create_with_context():
    """创建含有效触发原因和脱敏摘要的审核。

    检查两字段原样保存且状态待处理。
    """
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
    """使用不在允许集合中的审核原因。

    要求创建入口抛错，不保存未知原因。
    """
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
    """批准待审核记录并附备注与审核人。

    检查决定、状态、人员和处理时间均保存。
    """
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
    """拒绝一条待审核记录。

    检查状态转换为已拒绝。
    """
    _clean()
    svc = _svc()
    try:
        review = svc.create_with_context(1, 1, "t")
        result = svc.mark_decision(review.id, "reject")
        assert result.status == "rejected"
    finally:
        svc.db.close()

def test_mark_decision_refer():
    """提交转介对象和下一步说明。

    检查字段、用户提示及实际会话消息保存一致。
    """
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
    """提交持续关注负责人和时间。

    检查保存字段及用户提示中包含相同后续安排。
    """
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
    """只提交转介决定而没有行动详情。

    检查抛错并提示缺少转介目标。
    """
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
    """把转介对象填成空白。

    检查去空白后的无效字段不能被当作已填写。
    """
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
    """缺少负责人和时间时提交持续关注。

    检查拒绝并指出所需字段。
    """
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
    """提交未知审核决定。

    要求明确报错，避免状态进入未定义分支。
    """
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
    """先批准后再尝试拒绝同一条记录。

    检查已处理审核不能再次决定。
    """
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
    """创建轨迹上升触发的审核再读取列表。

    检查触发原因和脱敏摘要包含在返回字段中。
    """
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
    """遍历当前允许的全部触发原因创建审核。

    检查每种正式原因都能保存并原样读取。
    """
    _clean()
    svc = _svc()
    try:
        for reason in HANDOFF_REASONS:
            review = svc.create_with_context(1, 1, f"t-{reason}", handoff_reason=reason)
            assert review.handoff_reason == reason
    finally:
        svc.db.close()


# ---------------------------------------------------------------------------
# 本组测试结束。
# ---------------------------------------------------------------------------
