"""Tests for issue 07: Risk trajectory windows (3-msg session + 7-day cross-session).

Covers:
  - Recording trajectory points
  - Session window (last 3 points within same session)
  - Cross-session window (7-day rolling)
  - Rising detection (3 consecutive score increases)
  - Effective risk escalation (LOW -> MEDIUM, MEDIUM -> HIGH)
  - Explicit HIGH never downgraded
  - User/session isolation
  - Trajectory summary for admin (no sensitive content)

Run: python -m pytest tests/test_risk_trajectory.py
"""
from __future__ import annotations

from datetime import timedelta

from app.api.routes import health
from app.core.enums import RiskLevel
from app.core.time import utc_now
from app.models.entities import RiskTrajectoryPoint
from app.services.risk_trajectory import RiskTrajectoryHealth, RiskTrajectoryService
from tests.support import DatabaseHarness

_TestSession = DatabaseHarness().sessions


def _svc(**kwargs):
    """用新数据库会话构造可覆盖窗口参数的轨迹服务。

    返回连接供测试显式关闭。
    """
    db = _TestSession()
    return db, RiskTrajectoryService(db, **kwargs)


def _clean():
    """清除之前的轨迹记录并提交。

    避免不同测试之间共享风险趋势。
    """
    db = _TestSession()
    try:
        db.query(RiskTrajectoryPoint).delete()
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Recording points
# ---------------------------------------------------------------------------

def test_record_point_creates_entry():
    """记录一个指定用户和会话的风险点。

    检查编号、用户归属和风险分数正确；取得编号不代表单独提交。
    """
    _clean()
    db, svc = _svc()
    try:
        point = svc.record_point(1, 100, RiskLevel.LOW, 1.0)
        assert point.user_id == 1
        assert point.risk_level == "LOW"
        assert point.risk_score == 1.0
        assert point.id is not None
    finally:
        db.close()


def test_trajectory_health_reports_degraded_and_recovered_without_sensitive_data():
    """连续记录相同故障，再记录恢复成功。

    检查仅首次故障报告状态转变、恢复时间存在且快照不包含异常中的敏感正文。
    """
    RiskTrajectoryHealth.record_success()
    error = RuntimeError("student text must not be exposed")
    assert RiskTrajectoryHealth.record_failure(error) is True
    assert RiskTrajectoryHealth.record_failure(error) is False
    degraded = RiskTrajectoryHealth.snapshot()
    assert degraded["status"] == "degraded"
    assert degraded["lastErrorType"] == "RuntimeError"
    assert "student text" not in str(degraded)

    RiskTrajectoryHealth.record_success()
    recovered = RiskTrajectoryHealth.snapshot()
    assert recovered["status"] == "healthy"
    assert recovered["lastRecoveredAt"] is not None


def test_health_endpoint_includes_risk_trajectory_state():
    """直接读取健康接口结果。

    检查包含轨迹组件状态，并使用约定的未知、健康或降级值。
    """
    body = health()
    assert body["status"] == "UP"
    assert body["riskTrajectory"]["status"] in {"unknown", "healthy", "degraded"}


# ---------------------------------------------------------------------------
# Session window
# ---------------------------------------------------------------------------

def test_session_window_returns_last_3():
    """按顺序记录五个风险点。

    检查会话窗口只返回最近三个且最新在前。
    """
    _clean()
    db, svc = _svc()
    try:
        for score in [1.0, 2.0, 3.0, 4.0, 5.0]:
            svc.record_point(1, 100, RiskLevel.LOW, score)
        points = svc.get_session_points(1, 100)
        assert len(points) == 3  # session_window=3
        # newest first
        assert points[0].risk_score == 5.0
        assert points[2].risk_score == 3.0
    finally:
        db.close()


def test_session_window_isolates_sessions():
    """给同一用户的两个会话分别记录风险点。

    检查单会话查询不会混入另一会话。
    """
    _clean()
    db, svc = _svc()
    try:
        svc.record_point(1, 100, RiskLevel.LOW, 1.0)
        svc.record_point(1, 100, RiskLevel.LOW, 2.0)
        svc.record_point(1, 200, RiskLevel.LOW, 3.0)
        s100 = svc.get_session_points(1, 100)
        s200 = svc.get_session_points(1, 200)
        assert len(s100) == 2
        assert len(s200) == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Cross-session window (7 days)
# ---------------------------------------------------------------------------

def test_cross_session_window_7_days():
    """准备近期记录和八天前的旧记录。

    检查默认七天窗口排除过期项。
    """
    _clean()
    db, svc = _svc()
    try:
        # Recent point (within 7 days)
        svc.record_point(1, 100, RiskLevel.LOW, 1.0)
        # Old point (8 days ago) - insert manually
        old = RiskTrajectoryPoint(
            user_id=1, session_id=100, risk_level="LOW", risk_score=0.5,
        created_at=utc_now() - timedelta(days=8),
        )
        db.add(old)
        db.commit()
        points = svc.get_cross_session_points(1)
        assert len(points) == 1  # only the recent point
    finally:
        db.close()


def test_cross_session_isolates_users():
    """分别给两位用户记录风险点。

    检查跨会话查询仍按用户隔离。
    """
    _clean()
    db, svc = _svc()
    try:
        svc.record_point(1, 100, RiskLevel.LOW, 1.0)
        svc.record_point(2, 200, RiskLevel.LOW, 2.0)
        assert len(svc.get_cross_session_points(1)) == 1
        assert len(svc.get_cross_session_points(2)) == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Rising detection
# ---------------------------------------------------------------------------

def test_rising_detected_with_3_increases():
    """连续记录分数为一、二、三的三个点。

    检查三个严格上升的点满足阈值，这里实际是两次增加。
    """
    _clean()
    db, svc = _svc()
    try:
        for score in [1.0, 2.0, 3.0]:
            svc.record_point(1, 100, RiskLevel.LOW, score)
        points = svc.get_session_points(1, 100)
        assert svc.is_rising(points) is True
    finally:
        db.close()


def test_not_rising_with_fewer_than_threshold():
    """仅准备两个递增点。

    检查数量不足阈值时不认定持续上升。
    """
    _clean()
    db, svc = _svc()
    try:
        svc.record_point(1, 100, RiskLevel.LOW, 1.0)
        svc.record_point(1, 100, RiskLevel.LOW, 2.0)
        points = svc.get_session_points(1, 100)
        assert svc.is_rising(points) is False  # only 2 points, threshold=3
    finally:
        db.close()


def test_not_rising_when_decreasing():
    """准备三个连续下降点。

    检查趋势判断为非上升。
    """
    _clean()
    db, svc = _svc()
    try:
        for score in [3.0, 2.0, 1.0]:
            svc.record_point(1, 100, RiskLevel.LOW, score)
        points = svc.get_session_points(1, 100)
        assert svc.is_rising(points) is False
    finally:
        db.close()


def test_not_rising_when_flat():
    """准备三个相同分数点。

    检查相等不算严格上升。
    """
    _clean()
    db, svc = _svc()
    try:
        for score in [2.0, 2.0, 2.0]:
            svc.record_point(1, 100, RiskLevel.LOW, score)
        points = svc.get_session_points(1, 100)
        assert svc.is_rising(points) is False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Effective risk escalation
# ---------------------------------------------------------------------------

def test_high_never_downgraded():
    """历史分数下降但当前风险明确为高。

    检查轨迹不能降低已有高风险。
    """
    _clean()
    db, svc = _svc()
    try:
        # Even with a decreasing trajectory, HIGH stays HIGH
        for score in [4.0, 3.0, 2.0]:
            svc.record_point(1, 100, RiskLevel.LOW, score)
        effective = svc.get_effective_risk(1, 100, RiskLevel.HIGH, 4.0)
        assert effective == RiskLevel.HIGH
    finally:
        db.close()


def test_low_escalates_to_medium_on_rising():
    """前两点递增，再加入当前低风险分数形成上升段。

    检查有效风险提高一级到中。
    """
    _clean()
    db, svc = _svc()
    try:
        # Build a rising trajectory first
        svc.record_point(1, 100, RiskLevel.LOW, 1.0)
        svc.record_point(1, 100, RiskLevel.LOW, 2.0)
        # Now the third point triggers rising (1.0 -> 2.0 -> 3.0)
        effective = svc.get_effective_risk(1, 100, RiskLevel.LOW, 3.0)
        assert effective == RiskLevel.MEDIUM
    finally:
        db.close()


def test_medium_escalates_to_high_on_rising():
    """为中风险准备连续递增分数。

    检查有效风险提高到高等级。
    """
    _clean()
    db, svc = _svc()
    try:
        svc.record_point(1, 100, RiskLevel.MEDIUM, 2.0)
        svc.record_point(1, 100, RiskLevel.MEDIUM, 2.5)
        effective = svc.get_effective_risk(1, 100, RiskLevel.MEDIUM, 3.0)
        assert effective == RiskLevel.HIGH
    finally:
        db.close()


def test_low_stays_low_when_not_rising():
    """准备下降趋势并加入更低的当前分数。

    检查低风险保持不变。
    """
    _clean()
    db, svc = _svc()
    try:
        svc.record_point(1, 100, RiskLevel.LOW, 3.0)
        svc.record_point(1, 100, RiskLevel.LOW, 2.0)
        effective = svc.get_effective_risk(1, 100, RiskLevel.LOW, 1.0)
        assert effective == RiskLevel.LOW
    finally:
        db.close()


def test_insufficient_points_no_escalation():
    """历史只有一点，再加入当前点。

    检查点数不足时不提高风险。
    """
    _clean()
    db, svc = _svc()
    try:
        # Only 1 prior point, can't establish rising pattern
        svc.record_point(1, 100, RiskLevel.LOW, 1.0)
        effective = svc.get_effective_risk(1, 100, RiskLevel.LOW, 2.0)
        assert effective == RiskLevel.LOW
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Trajectory summary (admin view, no sensitive content)
# ---------------------------------------------------------------------------

def test_summary_no_data():
    """查询没有轨迹的用户。

    检查数量为零且趋势明确标为无数据。
    """
    _clean()
    db, svc = _svc()
    try:
        summary = svc.get_trajectory_summary(99)
        assert summary["totalPoints"] == 0
        assert summary["trend"] == "no_data"
    finally:
        db.close()


def test_summary_with_data():
    """记录三个低风险但分数递增的点后读取摘要。

    检查趋势为上升，而最新记录风险仍按已存值显示为低。
    """
    _clean()
    db, svc = _svc()
    try:
        svc.record_point(1, 100, RiskLevel.LOW, 1.0)
        svc.record_point(1, 100, RiskLevel.LOW, 2.0)
        svc.record_point(1, 100, RiskLevel.LOW, 3.0)
        summary = svc.get_trajectory_summary(1)
        assert summary["totalPoints"] >= 3
        assert summary["trend"] == "rising"
        assert summary["currentRisk"] == "LOW"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 本组测试结束。
# ---------------------------------------------------------------------------
