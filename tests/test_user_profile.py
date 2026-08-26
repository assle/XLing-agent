"""Tests for issue 03: User profile + exam stage context.

Covers:
  - UserProfileService CRUD (create, read, update, clear)
  - Exam stage validation (only 基础/强化/冲刺/考前/考后)
  - User isolation (cannot read/modify other users' profiles)
  - API endpoints (GET/PUT /api/profile/exam)
  - Stage context retrieval for agent runtime

Run:  python tests/test_user_profile.py
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
from app.models.entities import UserAccount, UserProfile
from app.services.user_profile import UserProfileService


# ---------------------------------------------------------------------------
# Test setup
# ---------------------------------------------------------------------------

_test_engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
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
        student = UserAccount(
            username="student",
            display_name="Test Student",
            password_hash=hash_password("student123"),
        )
        student.roles = {"ROLE_USER"}

        student2 = UserAccount(
            username="student2",
            display_name="Another Student",
            password_hash=hash_password("pass2"),
        )
        student2.roles = {"ROLE_USER"}

        db.add_all([student, student2])
        db.commit()
    finally:
        db.close()


_seed()
client = TestClient(app)


def _token(username="student", password="student123"):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    return r.json()["accessToken"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _clean_profiles():
    db = _TestSession()
    try:
        db.query(UserProfile).delete()
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Service: CRUD
# ---------------------------------------------------------------------------

def test_get_profile_returns_none_for_new_user():
    _clean_profiles()
    db = _TestSession()
    try:
        svc = UserProfileService(db)
        assert svc.get_profile(1) is None
    finally:
        db.close()


def test_update_profile_creates_if_not_exists():
    _clean_profiles()
    db = _TestSession()
    try:
        svc = UserProfileService(db)
        profile = svc.update_profile(1, exam_stage="基础", target_exam="考研", exam_date="2026-12-21")
        assert profile.exam_stage == "基础"
        assert profile.target_exam == "考研"
        assert profile.exam_date is not None
    finally:
        db.close()


def test_update_profile_modifies_existing():
    db = _TestSession()
    try:
        svc = UserProfileService(db)
        svc.update_profile(1, exam_stage="基础", target_exam="考研")
        profile = svc.update_profile(1, exam_stage="冲刺", target_exam="考公")
        assert profile.exam_stage == "冲刺"
        assert profile.target_exam == "考公"
    finally:
        db.close()


def test_update_profile_clears_optional_fields():
    _clean_profiles()
    db = _TestSession()
    try:
        svc = UserProfileService(db)
        svc.update_profile(1, exam_stage="基础", target_exam="考研", exam_date="2026-12-21")
        profile = svc.update_profile(1, exam_stage="", target_exam="", exam_date="")
        assert profile.exam_stage is None
        assert profile.target_exam is None
        assert profile.exam_date is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Service: exam stage validation
# ---------------------------------------------------------------------------

def test_invalid_exam_stage_raises():
    db = _TestSession()
    try:
        svc = UserProfileService(db)
        try:
            svc.update_profile(1, exam_stage="无效阶段")
            assert False, "Should have raised"
        except ValueError:
            pass
    finally:
        db.close()


def test_all_valid_stages_accepted():
    db = _TestSession()
    try:
        svc = UserProfileService(db)
        for stage in ["基础", "强化", "冲刺", "考前", "考后"]:
            profile = svc.update_profile(1, exam_stage=stage)
            assert profile.exam_stage == stage
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Service: user isolation
# ---------------------------------------------------------------------------

def test_user_isolation():
    _clean_profiles()
    db = _TestSession()
    try:
        svc = UserProfileService(db)
        svc.update_profile(1, exam_stage="基础", target_exam="考研")
        # User 2 has no profile
        assert svc.get_profile(2) is None
        # User 2 creates their own profile
        p2 = svc.update_profile(2, exam_stage="考后", target_exam="考公")
        assert p2.exam_stage == "考后"
        # User 1's profile is unchanged
        p1 = svc.get_profile(1)
        assert p1.exam_stage == "基础"
        assert p1.target_exam == "考研"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Service: stage context
# ---------------------------------------------------------------------------

def test_get_stage_context_returns_stage():
    db = _TestSession()
    try:
        svc = UserProfileService(db)
        svc.update_profile(1, exam_stage="冲刺")
        assert svc.get_stage_context(1) == "冲刺"
    finally:
        db.close()


def test_get_stage_context_empty_for_no_profile():
    db = _TestSession()
    try:
        svc = UserProfileService(db)
        assert svc.get_stage_context(99) == ""
    finally:
        db.close()


def test_get_stage_context_empty_for_no_stage():
    _clean_profiles()
    db = _TestSession()
    try:
        svc = UserProfileService(db)
        svc.update_profile(1, target_exam="考研")  # no stage
        assert svc.get_stage_context(1) == ""
    finally:
        db.close()


# ---------------------------------------------------------------------------
# API: GET /api/profile/exam
# ---------------------------------------------------------------------------

def test_api_get_profile_empty():
    _clean_profiles()
    token = _token()
    response = client.get("/api/profile/exam", headers=_auth(token))
    assert response.status_code == 200
    data = response.json()
    assert data["examStage"] is None
    assert data["targetExam"] is None


# ---------------------------------------------------------------------------
# API: PUT /api/profile/exam
# ---------------------------------------------------------------------------

def test_api_update_profile():
    token = _token()
    response = client.put("/api/profile/exam", json={
        "examStage": "冲刺", "targetExam": "考研", "examDate": "2026-12-21"
    }, headers=_auth(token))
    assert response.status_code == 200
    data = response.json()
    assert data["examStage"] == "冲刺"
    assert data["targetExam"] == "考研"
    assert data["examDate"] == "2026-12-21"


def test_api_update_profile_invalid_stage():
    token = _token()
    response = client.put("/api/profile/exam", json={
        "examStage": "无效"
    }, headers=_auth(token))
    assert response.status_code == 400


def test_api_update_then_get():
    token = _token()
    client.put("/api/profile/exam", json={
        "examStage": "考前", "targetExam": "考公"
    }, headers=_auth(token))
    response = client.get("/api/profile/exam", headers=_auth(token))
    assert response.status_code == 200
    data = response.json()
    assert data["examStage"] == "考前"
    assert data["targetExam"] == "考公"


def test_api_profile_user_isolation():
    token1 = _token("student", "student123")
    token2 = _token("student2", "pass2")

    client.put("/api/profile/exam", json={"examStage": "基础"}, headers=_auth(token1))
    client.put("/api/profile/exam", json={"examStage": "考后"}, headers=_auth(token2))

    r1 = client.get("/api/profile/exam", headers=_auth(token1))
    r2 = client.get("/api/profile/exam", headers=_auth(token2))
    assert r1.json()["examStage"] == "基础"
    assert r2.json()["examStage"] == "考后"


def test_api_profile_requires_auth():
    response = client.get("/api/profile/exam")
    assert response.status_code == 401


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
