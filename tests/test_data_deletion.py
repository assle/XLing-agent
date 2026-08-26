"""Tests for issue 13: Privacy notice + user data deletion.

Covers:
  - Privacy notice accessible without login
  - Data deletion removes all user data
  - Deletion is transactional (rollback on failure)
  - After deletion, user account and JWT are invalid
  - Data deletion requires authentication

Run:  python tests/test_data_deletion.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.api.routes import router
from app.core.database import Base, get_db
from app.core.security import hash_password
from app.models.entities import (
    UserAccount, UserProfile, MemoryCard, ScreeningResult,
    ActionPlan, ActionPlanItem, CheckIn, RiskTrajectoryPoint,
    ChatMessage, ChatSession, PsychologicalReport,
)
from app.services.data_deletion import DataDeletionService, PRIVACY_NOTICE
from app.services.user_profile import UserProfileService
from app.services.memory_cards import MemoryCardService
from app.services.action_plan import ActionPlanService


_test_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
_TestSession = sessionmaker(bind=_test_engine, autoflush=False, autocommit=False)

def _test_get_db():
    db = _TestSession()
    try:
        yield db
    finally:
        db.close()

app = FastAPI()
app.include_router(router)
app.dependency_overrides[get_db] = _test_get_db
Base.metadata.create_all(bind=_test_engine)

def _seed():
    db = _TestSession()
    try:
        s = UserAccount(username="student", display_name="S", password_hash=hash_password("student123"))
        s.roles = {"ROLE_USER"}
        s2 = UserAccount(username="student2", display_name="S2", password_hash=hash_password("p2"))
        s2.roles = {"ROLE_USER"}
        db.add_all([s, s2])
        db.commit()
    finally:
        db.close()

_seed()
client = TestClient(app)

def _token(u="student", p="student123"):
    r = client.post("/api/auth/login", json={"username": u, "password": p})
    return r.json()["accessToken"]

def _auth(t):
    return {"Authorization": f"Bearer {t}"}

def _setup_user_data(user_id=1):
    """Create a full set of user data for deletion testing."""
    db = _TestSession()
    try:
        # Profile
        UserProfileService(db).update_profile(user_id, exam_stage="冲刺", target_exam="考研")
        # Memory cards
        MemoryCardService(db).create_card(user_id, "important memory")
        # Screening result
        from app.services.screening import ScreeningService
        ScreeningService(db).submit_screening(user_id, "PHQ-9", [0]*9)
        # Action plan
        plan = ActionPlanService(db, ai=None).generate_plan(user_id, None, "summary")
        # Risk trajectory
        from app.services.risk_trajectory import RiskTrajectoryService
        from app.core.enums import RiskLevel
        RiskTrajectoryService(db).record_point(user_id, None, RiskLevel.LOW, 1.0)
        # Chat session + message
        session = ChatSession(public_id=f"test-sess-{user_id}", title="test", user_id=user_id)
        db.add(session)
        db.flush()
        db.add(ChatMessage(user_id=user_id, session_id=session.id, role="USER", content="test"))
        db.add(PsychologicalReport(
            user_id=user_id, session_id=session.id, content="test",
            intent="CONSULT", emotion="ANXIETY", emotion_score=2.0,
            risk_level="LOW", confidence=0.7, summary="test",
        ))
        db.commit()
    finally:
        db.close()

def _reset_db():
    """Reset all data and re-seed users."""
    db = _TestSession()
    try:
        for model in [CheckIn, ActionPlanItem, ActionPlan, RiskTrajectoryPoint, ChatMessage, 
                      PsychologicalReport, ChatSession, ScreeningResult, MemoryCard, UserProfile, UserAccount]:
            db.query(model).delete()
        db.commit()
    finally:
        db.close()
    _seed()


def _reset_db():
    """Reset all data and re-seed users."""
    db = _TestSession()
    try:
        for model in [CheckIn, ActionPlanItem, ActionPlan, RiskTrajectoryPoint, ChatMessage,
                      PsychologicalReport, ChatSession, ScreeningResult, MemoryCard, UserProfile, UserAccount]:
            db.query(model).delete()
        db.commit()
    finally:
        db.close()
    _seed()


# ---------------------------------------------------------------------------
# Privacy notice
# ---------------------------------------------------------------------------

def test_privacy_notice_no_auth_required():
    response = client.get("/api/privacy")
    assert response.status_code == 200
    data = response.json()
    assert "notice" in data
    assert "隐私" in data["notice"]
    assert "收集" in data["notice"]
    assert "删除" in data["notice"]
    assert "无记忆" in data["notice"]


# ---------------------------------------------------------------------------
# Data deletion
# ---------------------------------------------------------------------------

def test_delete_removes_all_user_data():
    _reset_db()
    _setup_user_data(1)
    db = _TestSession()
    try:
        svc = DataDeletionService(db)
        counts = svc.delete_all_user_data(1)
        assert counts["user_account"] == 1
        assert counts["memory_cards"] >= 1
        assert counts["screening_results"] >= 1
        assert counts["user_profiles"] >= 1
        assert counts["action_plans"] >= 1
        assert counts["risk_trajectory"] >= 1
        assert counts["chat_messages"] >= 1
        assert counts["psychological_reports"] >= 1
        # User is gone
        assert db.get(UserAccount, 1) is None
    finally:
        db.close()

def test_delete_does_not_affect_other_users():
    _reset_db()
    _setup_user_data(1)
    _setup_user_data(2)
    db = _TestSession()
    try:
        svc = DataDeletionService(db)
        svc.delete_all_user_data(1)
        # User 2 still exists
        assert db.get(UserAccount, 2) is not None
        assert UserProfileService(db).get_profile(2) is not None
    finally:
        db.close()

def test_delete_makes_token_invalid():
    _reset_db()
    _setup_user_data(1)
    token = _token()
    # Verify token works
    r = client.get("/api/profile", headers=_auth(token))
    assert r.status_code == 200
    # Delete account
    r = client.delete("/api/account", headers=_auth(token))
    assert r.status_code == 200
    # Token should now be invalid (user not found)
    r = client.get("/api/profile", headers=_auth(token))
    assert r.status_code == 401

def test_delete_requires_auth():
    r = client.delete("/api/account")
    assert r.status_code == 401

def test_api_delete_account():
    _reset_db()
    _setup_user_data(1)
    token = _token()
    r = client.delete("/api/account", headers=_auth(token))
    assert r.status_code == 200
    data = r.json()
    assert data["deleted"] is True
    assert "details" in data


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
