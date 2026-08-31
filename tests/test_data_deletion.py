"""Tests for issue 13: Privacy notice + user data deletion.

Covers:
  - Privacy notice accessible without login
  - Data deletion removes all user data
  - Deletion is transactional (rollback on failure)
  - After deletion, user account and JWT are invalid
  - Data deletion requires authentication

Run: python -m pytest tests/test_data_deletion.py
"""
from __future__ import annotations

import sqlite3

import pytest

from app.api.routes import router
from app.core.config import Settings
from app.core.security import hash_password
from app.models.entities import (
    ActionPlan,
    ActionPlanItem,
    ChatMessage,
    ChatSession,
    CheckIn,
    CheckpointDeletionTask,
    MemoryCard,
    RiskTrajectoryPoint,
    SafetyAssessmentRecord,
    ScreeningResult,
    UserAccount,
    UserProfile,
)
from app.services.action_plan import ActionPlanService
from app.services.data_deletion import DataDeletionService
from app.services.memory_cards import MemoryCardService
from app.services.user_profile import UserProfileService
from tests.support import ApiHarness

_harness = ApiHarness(router)
_TestSession = _harness.sessions
client = _harness.client

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
        UserProfileService(db).update_support_background(user_id, current_concern="近期压力较大")
        # Memory cards
        MemoryCardService(db).create_card(user_id, "important memory")
        # Screening result
        from app.services.screening import ScreeningService
        ScreeningService(db).submit_screening(user_id, "PHQ-9", [0]*9)
        # Action plan
        ActionPlanService(db, ai=None).generate_plan(user_id, None, "summary")
        # Risk trajectory
        from app.core.enums import RiskLevel
        from app.services.risk_trajectory import RiskTrajectoryService
        RiskTrajectoryService(db).record_point(user_id, None, RiskLevel.LOW, 1.0)
        # Chat session + message
        session = ChatSession(public_id=f"test-sess-{user_id}", title="test", user_id=user_id)
        db.add(session)
        db.flush()
        db.add(ChatMessage(user_id=user_id, session_id=session.id, role="USER", content="test"))
        db.add(SafetyAssessmentRecord(
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
                      SafetyAssessmentRecord, ChatSession, ScreeningResult, MemoryCard, UserProfile, UserAccount]:
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
                      SafetyAssessmentRecord, ChatSession, ScreeningResult, MemoryCard, UserProfile, UserAccount]:
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
        assert counts["safety_assessment_records"] >= 1
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


def test_data_deletion_removes_persistent_checkpoints(tmp_path):
    _reset_db()
    _setup_user_data(1)
    db = _TestSession()
    checkpoint_path = tmp_path / "checkpoints.db"
    try:
        thread_id = db.query(ChatSession).filter(ChatSession.user_id == 1).one().public_id
        connection = sqlite3.connect(checkpoint_path)
        connection.executescript(
            """
            CREATE TABLE checkpoints (
                thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT,
                parent_checkpoint_id TEXT, type TEXT, checkpoint BLOB, metadata BLOB
            );
            CREATE TABLE writes (
                thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT,
                task_id TEXT, idx INTEGER, channel TEXT, type TEXT, value BLOB
            );
            """
        )
        connection.execute(
            "INSERT INTO checkpoints(thread_id, checkpoint_id) VALUES (?, ?)",
            (thread_id, "checkpoint-1"),
        )
        connection.execute(
            "INSERT INTO writes(thread_id, checkpoint_id) VALUES (?, ?)",
            (thread_id, "checkpoint-1"),
        )
        connection.commit()
        connection.close()

        DataDeletionService(
            db,
            Settings(
                langgraph_checkpoint_backend="async_sqlite",
                langgraph_checkpoint_path=str(checkpoint_path),
            ),
        ).delete_all_user_data(1)

        connection = sqlite3.connect(checkpoint_path)
        assert connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM writes").fetchone()[0] == 0
        connection.close()
    finally:
        db.close()


def test_business_rollback_does_not_delete_checkpoints(tmp_path, monkeypatch):
    _reset_db()
    _setup_user_data(1)
    db = _TestSession()
    checkpoint_path = tmp_path / "checkpoints.db"
    try:
        thread_id = (
            db.query(ChatSession)
            .filter(ChatSession.user_id == 1)
            .one()
            .public_id
        )
        connection = sqlite3.connect(checkpoint_path)
        connection.execute(
            "CREATE TABLE checkpoints (thread_id TEXT, checkpoint_id TEXT)"
        )
        connection.execute(
            "INSERT INTO checkpoints(thread_id, checkpoint_id) VALUES (?, ?)",
            (thread_id, "checkpoint-1"),
        )
        connection.commit()
        connection.close()

        def fail_commit():
            raise RuntimeError("business commit failed")

        monkeypatch.setattr(db, "commit", fail_commit)

        with pytest.raises(RuntimeError, match="business commit failed"):
            DataDeletionService(
                db,
                Settings(
                    langgraph_checkpoint_backend="async_sqlite",
                    langgraph_checkpoint_path=str(checkpoint_path),
                ),
            ).delete_all_user_data(1)

        connection = sqlite3.connect(checkpoint_path)
        assert connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 1
        connection.close()
        assert db.get(UserAccount, 1) is not None
    finally:
        db.close()


def test_failed_checkpoint_cleanup_is_persisted_and_retried(tmp_path, monkeypatch):
    _reset_db()
    _setup_user_data(1)
    db = _TestSession()
    checkpoint_path = tmp_path / "checkpoints.db"
    try:
        thread_id = (
            db.query(ChatSession)
            .filter(ChatSession.user_id == 1)
            .one()
            .public_id
        )
        connection = sqlite3.connect(checkpoint_path)
        connection.execute(
            "CREATE TABLE checkpoints (thread_id TEXT, checkpoint_id TEXT)"
        )
        connection.execute(
            "INSERT INTO checkpoints(thread_id, checkpoint_id) VALUES (?, ?)",
            (thread_id, "checkpoint-1"),
        )
        connection.commit()
        connection.close()
        service = DataDeletionService(
            db,
            Settings(
                langgraph_checkpoint_backend="async_sqlite",
                langgraph_checkpoint_path=str(checkpoint_path),
            ),
        )
        original_delete = service._delete_checkpoint_rows

        def fail_cleanup(thread_ids, path):
            raise sqlite3.OperationalError("database busy")

        monkeypatch.setattr(service, "_delete_checkpoint_rows", fail_cleanup)

        counts = service.delete_all_user_data(1)

        assert counts["checkpoint_cleanup_pending"] == 1
        task = db.query(CheckpointDeletionTask).one()
        assert task.attempts == 1
        assert db.get(UserAccount, 1) is None

        monkeypatch.setattr(service, "_delete_checkpoint_rows", original_delete)
        assert service.retry_pending_checkpoint_deletions() == 1
        assert db.query(CheckpointDeletionTask).count() == 0
        connection = sqlite3.connect(checkpoint_path)
        assert connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0
        connection.close()
    finally:
        db.close()
