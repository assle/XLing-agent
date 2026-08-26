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

Run:  python tests/test_risk_trajectory.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.core.enums import RiskLevel
from app.models.entities import RiskTrajectoryPoint
from app.services.risk_trajectory import RiskTrajectoryService


_test_engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_TestSession = sessionmaker(bind=_test_engine, autoflush=False, autocommit=False)
Base.metadata.create_all(bind=_test_engine)


def _svc(**kwargs):
    db = _TestSession()
    return db, RiskTrajectoryService(db, **kwargs)


def _clean():
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


# ---------------------------------------------------------------------------
# Session window
# ---------------------------------------------------------------------------

def test_session_window_returns_last_3():
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
    _clean()
    db, svc = _svc()
    try:
        # Recent point (within 7 days)
        svc.record_point(1, 100, RiskLevel.LOW, 1.0)
        # Old point (8 days ago) - insert manually
        old = RiskTrajectoryPoint(
            user_id=1, session_id=100, risk_level="LOW", risk_score=0.5,
            created_at=datetime.utcnow() - timedelta(days=8),
        )
        db.add(old)
        db.commit()
        points = svc.get_cross_session_points(1)
        assert len(points) == 1  # only the recent point
    finally:
        db.close()


def test_cross_session_isolates_users():
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
    _clean()
    db, svc = _svc()
    try:
        summary = svc.get_trajectory_summary(99)
        assert summary["totalPoints"] == 0
        assert summary["trend"] == "no_data"
    finally:
        db.close()


def test_summary_with_data():
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
# Runner
# ---------------------------------------------------------------------------

_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    passed = 0
    failed = 0
    for test in _TESTS:
        try:
            test()
            print(f"  PASS  {test.__name__}")
            passed += 1
        except AssertionError as exc:
            print(f"  FAIL  {test.__name__}: {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERROR {test.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(_TESTS)} total")
    sys.exit(1 if failed else 0)
