from __future__ import annotations

import threading
from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.enums import RiskLevel
from app.core.time import utc_now
from app.models.entities import RiskTrajectoryPoint

_RISK_ORDER = {RiskLevel.LOW: 1, RiskLevel.MEDIUM: 2, RiskLevel.HIGH: 3}


class RiskTrajectoryHealth:
    """保存当前进程的轨迹组件健康状态，不包含用户表达或风险详情。"""

    _lock = threading.Lock()
    _status = "unknown"
    _failure_count = 0
    _last_error_type: str | None = None
    _last_failure_at: str | None = None
    _last_success_at: str | None = None
    _last_recovered_at: str | None = None

    @classmethod
    def record_success(cls) -> None:
        """记录轨迹组件最近成功时间，并在从降级恢复时记录恢复时间。

        使用共享锁保护状态，只保存在当前进程，不清零累计失败次数。
        """
        now = utc_now().isoformat()
        with cls._lock:
            if cls._status == "degraded":
                cls._last_recovered_at = now
            cls._status = "healthy"
            cls._last_success_at = now

    @classmethod
    def record_failure(cls, error: Exception) -> bool:
        """记录异常类型和时间，并返回是否刚进入降级状态。

        返回 True 可用于仅在状态转变时打印醒目日志；不保存用户正文或具体风险内容。
        """
        now = utc_now().isoformat()
        with cls._lock:
            changed = cls._status != "degraded"
            cls._status = "degraded"
            cls._failure_count += 1
            cls._last_error_type = type(error).__name__
            cls._last_failure_at = now
            return changed

    @classmethod
    def snapshot(cls) -> dict:
        """在共享锁保护下返回组件运行状态快照。

        只包含计数、时间和异常类型，不查询数据库或主动检测连接。
        """
        with cls._lock:
            return {
                "status": cls._status,
                "failureCount": cls._failure_count,
                "lastErrorType": cls._last_error_type,
                "lastFailureAt": cls._last_failure_at,
                "lastSuccessAt": cls._last_success_at,
                "lastRecoveredAt": cls._last_recovered_at,
            }


class RiskTrajectoryService:
    """结合会话内和跨会话的近期风险分数识别持续上升趋势。"""

    def __init__(self, db: Session, session_window: int = 3, cross_session_days: int = 7, rising_threshold: int = 3):
        """保存会话窗口、跨会话天数和连续上升阈值。

        db 用于轨迹读写；参数由调用方或配置提供，初始化不记录风险点。
        """
        self.db = db
        self.session_window = session_window
        self.cross_session_days = cross_session_days
        self.rising_threshold = rising_threshold

    def record_point(self, user_id: int, session_id: int | None, risk: RiskLevel, score: float) -> RiskTrajectoryPoint:
        """为当前用户和可选会话准备一条风险轨迹记录。

        flush 后取得数据库值，但不提交，允许外层与本轮其他记录一起确认保存。
        """
        point = RiskTrajectoryPoint(
            user_id=user_id,
            session_id=session_id,
            risk_level=risk.value,
            risk_score=score,
        )
        self.db.add(point)
        self.db.flush()
        self.db.refresh(point)
        return point

    def get_session_points(self, user_id: int, session_id: int) -> list[RiskTrajectoryPoint]:
        """查询指定用户同一会话中最近的若干风险点。

        数量受 session_window 限制，返回顺序为最新在前。
        """
        return (
            self.db.query(RiskTrajectoryPoint)
            .filter(RiskTrajectoryPoint.user_id == user_id)
            .filter(RiskTrajectoryPoint.session_id == session_id)
            .order_by(RiskTrajectoryPoint.created_at.desc())
            .limit(self.session_window)
            .all()
        )

    def get_cross_session_points(self, user_id: int) -> list[RiskTrajectoryPoint]:
        """查询指定用户最近配置天数内的所有风险点。

        不限制会话编号，按最新在前排列，供跨会话趋势判断。
        """
        cutoff = utc_now() - timedelta(days=self.cross_session_days)
        return (
            self.db.query(RiskTrajectoryPoint)
            .filter(RiskTrajectoryPoint.user_id == user_id)
            .filter(RiskTrajectoryPoint.created_at >= cutoff)
            .order_by(RiskTrajectoryPoint.created_at.desc())
            .all()
        )

    def is_rising(self, points: list[RiskTrajectoryPoint]) -> bool:
        """判断截至最新点的连续严格上升段是否达到阈值。

        输入须为最新在前；先反转成时间顺序，相等或下降就把连续长度重置为一。
        统计的是连续点数，不是历史上任意一段上升，也不是增加次数。
        """
        if len(points) < self.rising_threshold:
            return False
        # 输入由新到旧，反转后才能沿时间前进方向比较分数。
        chronological = list(reversed(points))
        # 相等或下降都会打断连续上升，重新从当前点算起。
        consecutive_rising = 1
        for i in range(1, len(chronological)):
            if chronological[i].risk_score > chronological[i - 1].risk_score:
                consecutive_rising += 1
            else:
                consecutive_rising = 1
        return consecutive_rising >= self.rising_threshold

    def get_effective_risk(
        self, user_id: int, session_id: int | None, current_risk: RiskLevel, current_score: float
    ) -> RiskLevel:
        """结合当前分数与会话内、跨会话趋势决定有效风险。

        已是高风险时立即返回且不在此记录新点；其他情况先记录当前点，再判断趋势。
        任一窗口持续上升时只提高一级，不降低当前风险。
        """
        # 已明确高风险时直接保留，不因历史趋势改善而降低。
        if current_risk == RiskLevel.HIGH:
            return RiskLevel.HIGH

        # 把当前分数也加入趋势窗口；此处尚未单独提交记录。
        self.record_point(user_id, session_id, current_risk, current_score)

        # 检查同一会话内的近期风险点。
        session_points = self.get_session_points(user_id, session_id) if session_id else []
        session_rising = self.is_rising(session_points)

        # 再检查该用户跨会话的近期风险点。
        cross_points = self.get_cross_session_points(user_id)
        cross_rising = self.is_rising(cross_points)

        # 两个观察窗口任一满足条件就提高一级，不需要同时成立。
        if session_rising or cross_rising:
            # 每次只提高一级：低到中，中到高。
            if current_risk == RiskLevel.LOW:
                return RiskLevel.MEDIUM
            if current_risk == RiskLevel.MEDIUM:
                return RiskLevel.HIGH

        return current_risk

    def get_trajectory_summary(self, user_id: int) -> dict:
        """返回近期轨迹数量、趋势及最新风险，不包含消息正文。

        短窗口从有会话编号的近期点中截取，可能包含不同会话；跨会话窗口同时参与趋势判断。
        """
        cross_points = self.get_cross_session_points(user_id)
        if not cross_points:
            return {"totalPoints": 0, "trend": "no_data", "currentRisk": None}
        session_rising = self.is_rising(
            [p for p in cross_points if p.session_id is not None][:self.session_window]
        )
        cross_rising = self.is_rising(cross_points)
        trend = "rising" if (session_rising or cross_rising) else "stable"
        latest = cross_points[0]
        return {
            "totalPoints": len(cross_points),
            "trend": trend,
            "currentRisk": latest.risk_level,
            "latestScore": latest.risk_score,
        }
