from __future__ import annotations

import threading
from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.enums import RiskLevel
from app.core.time import utc_now
from app.models.entities import RiskTrajectoryPoint

_RISK_ORDER = {RiskLevel.LOW: 1, RiskLevel.MEDIUM: 2, RiskLevel.HIGH: 3}


class RiskTrajectoryHealth:
    """Process-local health state for risk trajectory evaluation.

    The snapshot intentionally contains only operational metadata, never
    student content or risk details.
    """

    _lock = threading.Lock()
    _status = "unknown"
    _failure_count = 0
    _last_error_type: str | None = None
    _last_failure_at: str | None = None
    _last_success_at: str | None = None
    _last_recovered_at: str | None = None

    @classmethod
    def record_success(cls) -> None:
        now = utc_now().isoformat()
        with cls._lock:
            if cls._status == "degraded":
                cls._last_recovered_at = now
            cls._status = "healthy"
            cls._last_success_at = now

    @classmethod
    def record_failure(cls, error: Exception) -> bool:
        """Record a failure and return whether this is a new degraded state."""
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
    """Tracks risk assessment trends across session and cross-session windows.

    Issue 07: session window = last 3 messages, cross-session = last 7 days.
    Rising trajectory escalates effective risk; explicit HIGH is never downgraded.
    """

    def __init__(self, db: Session, session_window: int = 3, cross_session_days: int = 7, rising_threshold: int = 3):
        self.db = db
        self.session_window = session_window
        self.cross_session_days = cross_session_days
        self.rising_threshold = rising_threshold

    def record_point(self, user_id: int, session_id: int | None, risk: RiskLevel, score: float) -> RiskTrajectoryPoint:
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
        """Last N trajectory points within the same session."""
        return (
            self.db.query(RiskTrajectoryPoint)
            .filter(RiskTrajectoryPoint.user_id == user_id)
            .filter(RiskTrajectoryPoint.session_id == session_id)
            .order_by(RiskTrajectoryPoint.created_at.desc())
            .limit(self.session_window)
            .all()
        )

    def get_cross_session_points(self, user_id: int) -> list[RiskTrajectoryPoint]:
        """All trajectory points in the last N days for this user."""
        cutoff = utc_now() - timedelta(days=self.cross_session_days)
        return (
            self.db.query(RiskTrajectoryPoint)
            .filter(RiskTrajectoryPoint.user_id == user_id)
            .filter(RiskTrajectoryPoint.created_at >= cutoff)
            .order_by(RiskTrajectoryPoint.created_at.desc())
            .all()
        )

    def is_rising(self, points: list[RiskTrajectoryPoint]) -> bool:
        """Check if risk scores are strictly increasing across consecutive points."""
        if len(points) < self.rising_threshold:
            return False
        # points are ordered desc (newest first); reverse for chronological
        chronological = list(reversed(points))
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
        """Compute effective risk considering trajectory trends.

        - Explicit HIGH is never downgraded.
        - Rising trajectory (>= threshold consecutive increases) escalates risk.
        """
        # Explicit HIGH is never downgraded
        if current_risk == RiskLevel.HIGH:
            return RiskLevel.HIGH

        # Record the current point for trajectory analysis
        self.record_point(user_id, session_id, current_risk, current_score)

        # Check session window
        session_points = self.get_session_points(user_id, session_id) if session_id else []
        session_rising = self.is_rising(session_points)

        # Check cross-session window
        cross_points = self.get_cross_session_points(user_id)
        cross_rising = self.is_rising(cross_points)

        if session_rising or cross_rising:
            # Escalate: LOW -> MEDIUM, MEDIUM -> HIGH
            if current_risk == RiskLevel.LOW:
                return RiskLevel.MEDIUM
            if current_risk == RiskLevel.MEDIUM:
                return RiskLevel.HIGH

        return current_risk

    def get_trajectory_summary(self, user_id: int) -> dict:
        """Admin-viewable summary without sensitive content."""
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
