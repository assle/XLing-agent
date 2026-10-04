"""Tests for issue 12: Risk-triggered closed-loop escalation.

Covers:
  - Trajectory rising -> RISK_TRAJECTORY_RISING escalation
  - HIGH risk -> no trajectory escalation (keyword path handles it)
  - Check-in worsened -> SUSTAINED_NO_IMPROVEMENT escalation
  - Check-in improved -> no escalation
  - Screening high-risk -> HIGH_RISK_KEYWORD escalation
  - User request -> USER_REQUEST escalation
  - All escalations have desensitized summary + safety message
  - Screening suggestion is voluntary (not forced)

Run: python -m pytest tests/test_escalation.py
"""
from __future__ import annotations

from app.core.config import Settings
from app.core.enums import RiskLevel
from app.core.security import hash_password
from app.models.entities import ChatSession, ReviewRequest, SafetyAssessmentRecord, UserAccount
from app.services.escalation import SAFETY_MESSAGE, SCREENING_SUGGESTION, EscalationService
from tests.support import DatabaseHarness

_TestSession = DatabaseHarness().sessions
_settings = Settings()


def _seed():
    """建立普通用户、会话和中风险评估记录。

    为不同升级触发条件提供可关联的数据。
    """
    db = _TestSession()
    try:
        s = UserAccount(username="student", display_name="S", password_hash=hash_password("s"))
        s.roles = {"ROLE_USER"}
        session = ChatSession(public_id="sess-1", title="test", user_id=1)
        report = SafetyAssessmentRecord(
            user_id=1, session_id=1, content="test",
            intent="CONSULT", emotion="ANXIETY", emotion_score=2.5,
            risk_level="MEDIUM", confidence=0.8, summary="anxiety",
        )
        db.add_all([s, session, report])
        db.commit()
    finally:
        db.close()

_seed()


def _svc():
    """新建数据库会话并把审核服务注入升级服务。

    返回会话与服务，便于调用结束后清理。
    """
    from app.services.review import ReviewService
    db = _TestSession()
    return db, EscalationService(db, ReviewService(db, _settings))

def _clean():
    """只删除此前创建的审核记录。

    保留共同的用户、会话和评估基础。
    """
    db = _TestSession()
    try:
        db.query(ReviewRequest).delete()
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Trajectory escalation
# ---------------------------------------------------------------------------

def test_trajectory_rising_escalates():
    """单次为中风险但轨迹上升时检查升级。

    要求创建审核编号，并带正确原因、困境摘要和安全提示。
    """
    _clean()
    db, svc = _svc()
    try:
        result = svc.check_trajectory_escalation(
            1, 1, 1, "thread-1", RiskLevel.MEDIUM, True,
            current_difficulty="考研焦虑加剧", risk_trend="rising",
        )
        assert result.should_escalate is True
        assert result.handoff_reason == "RISK_TRAJECTORY_RISING"
        assert result.review_id is not None
        assert SAFETY_MESSAGE in result.user_message
        assert "当前困境" in result.desensitized_summary
    finally:
        db.close()

def test_high_risk_no_trajectory_escalation():
    """当前已明确高风险时调用轨迹升级入口。

    检查它不重复处理由其他高风险路径负责的情况。
    """
    _clean()
    db, svc = _svc()
    try:
        result = svc.check_trajectory_escalation(
            1, 1, 1, "thread-1", RiskLevel.HIGH, True,
        )
        assert result.should_escalate is False  # HIGH handled by keyword path
    finally:
        db.close()

def test_not_rising_no_escalation():
    """当前低风险且轨迹未上升。

    检查无需创建升级处理。
    """
    _clean()
    db, svc = _svc()
    try:
        result = svc.check_trajectory_escalation(
            1, 1, 1, "thread-1", RiskLevel.LOW, False,
        )
        assert result.should_escalate is False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Check-in escalation
# ---------------------------------------------------------------------------

def test_checkin_worsened_escalates():
    """提交恶化状态给次日反馈升级入口。

    检查升级标志及未改善原因类别。
    """
    _clean()
    db, svc = _svc()
    try:
        result = svc.check_checkin_escalation(
            1, 1, 1, "thread-1", "worsened",
            current_difficulty="情况恶化",
        )
        assert result.should_escalate is True
        assert result.handoff_reason == "SUSTAINED_NO_IMPROVEMENT"
    finally:
        db.close()

def test_checkin_unchanged_escalates():
    """传入一次没有改善的反馈。

    检查当前规则直接要求升级，不在本函数累计连续次数。
    """
    _clean()
    db, svc = _svc()
    try:
        result = svc.check_checkin_escalation(
            1, 1, 1, "thread-1", "unchanged",
        )
        assert result.should_escalate is True
        assert result.handoff_reason == "SUSTAINED_NO_IMPROVEMENT"
    finally:
        db.close()

def test_checkin_improved_no_escalation():
    """传入改善反馈。

    检查不会进入安全升级。
    """
    _clean()
    db, svc = _svc()
    try:
        result = svc.check_checkin_escalation(
            1, 1, 1, "thread-1", "improved",
        )
        assert result.should_escalate is False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Screening escalation
# ---------------------------------------------------------------------------

def test_screening_high_risk_escalates():
    """把筛查需立即关注标志设为真。

    检查产生升级并使用当前约定的高风险原因。
    """
    _clean()
    db, svc = _svc()
    try:
        result = svc.check_screening_escalation(
            1, 1, 1, "thread-1", True,
            current_difficulty="PHQ-9 Q9 self-harm",
        )
        assert result.should_escalate is True
        assert result.handoff_reason == "HIGH_RISK_KEYWORD"
    finally:
        db.close()

def test_screening_no_high_risk_no_escalation():
    """筛查没有需立即关注的答案标志。

    检查返回无需升级。
    """
    _clean()
    db, svc = _svc()
    try:
        result = svc.check_screening_escalation(
            1, 1, 1, "thread-1", False,
        )
        assert result.should_escalate is False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# User request escalation
# ---------------------------------------------------------------------------

def test_user_request_escalates():
    """模拟用户主动请求人工支持。

    检查对应原因和实际审核编号。
    """
    _clean()
    db, svc = _svc()
    try:
        result = svc.user_request_escalation(
            1, 1, 1, "thread-1",
            current_difficulty="用户主动请求真人支持",
        )
        assert result.should_escalate is True
        assert result.handoff_reason == "USER_REQUEST"
        assert result.review_id is not None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# All escalations have desensitized summary + safety message
# ---------------------------------------------------------------------------

def test_all_escalations_have_safety_message():
    """分别触发轨迹、反馈、筛查和主动求助升级。

    检查每种路径都有安全提示和非空摘要。
    """
    _clean()
    db, svc = _svc()
    try:
        for result in [
            svc.check_trajectory_escalation(1, 1, 1, "t", RiskLevel.MEDIUM, True),
            svc.check_checkin_escalation(1, 1, 1, "t", "worsened"),
            svc.check_screening_escalation(1, 1, 1, "t", True),
            svc.user_request_escalation(1, 1, 1, "t"),
        ]:
            assert result.should_escalate is True
            assert SAFETY_MESSAGE in result.user_message
            assert len(result.desensitized_summary) > 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Screening suggestion is voluntary
# ---------------------------------------------------------------------------

def test_screening_suggestion_is_message():
    """读取筛查邀请文字。

    检查它明确包含自愿含义并复用统一常量。
    """
    suggestion = EscalationService.get_screening_suggestion()
    assert "自愿" in suggestion
    assert SCREENING_SUGGESTION == suggestion

def test_safety_message_uses_configurable_resource_category():
    """检查统一安全提示包含当地紧急服务。

    避免依赖固定学校名称作为唯一资源。
    """
    assert "当地紧急服务" in SAFETY_MESSAGE


# ---------------------------------------------------------------------------
# Production-wiring behavior (closed-loop fix)
# ---------------------------------------------------------------------------

def test_checkin_escalation_creates_labeled_report_when_missing():
    """反馈升级没有已有评估编号时继续处理。

    检查新建关联记录明确标注次日反馈来源，置信度为零。
    """
    _clean()
    db, svc = _svc()
    try:
        result = svc.check_checkin_escalation(
            1, 1, None, "thread-1", "worsened",
            current_difficulty="坚持不下去了",
        )
        assert result.should_escalate is True
        assert result.review_id is not None
        review = db.get(ReviewRequest, result.review_id)
        assert review.handoff_reason == "SUSTAINED_NO_IMPROVEMENT"
        report = db.get(SafetyAssessmentRecord, review.report_id)
        assert report is not None
        assert "次日反馈" in report.summary
        assert report.confidence == 0.0
    finally:
        db.close()

def test_checkin_escalation_without_session_skips_review():
    """反馈恶化但缺少会话和执行编号。

    检查内部仍表达需要支持，却不创建审核记录，避免伪造关联。
    """
    _clean()
    db, svc = _svc()
    try:
        result = svc.check_checkin_escalation(1, None, None, "", "worsened")
        assert result.should_escalate is True
        assert result.review_id is None
        assert SAFETY_MESSAGE in result.user_message
        assert db.query(ReviewRequest).count() == 0
    finally:
        db.close()

def test_checkin_summary_uses_chinese_status():
    """传入没有改善的状态代码。

    检查审核摘要使用对应中文表述。
    """
    _clean()
    db, svc = _svc()
    try:
        result = svc.check_checkin_escalation(1, 1, 1, "thread-1", "unchanged")
        assert "没有改善" in result.desensitized_summary
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 本组测试结束。
# ---------------------------------------------------------------------------
