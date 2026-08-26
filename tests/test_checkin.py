"""Tests for issue 10: Next-day check-in.

Covers:
  - Pending plans detection
  - Submit check-in (improved/unchanged/worsened)
  - Idempotent submission (update existing)
  - Plan status update based on check-in result
  - User isolation
  - API endpoints

Run:  python tests/test_checkin.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
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
from app.models.entities import ChatSession, PsychologicalReport, ReviewRequest, UserAccount, ActionPlan, ActionPlanItem, CheckIn
from app.services.action_plan import ActionPlanService
from app.services.checkin import CheckInService


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

def _clean():
    db = _TestSession()
    try:
        db.query(ReviewRequest).delete()
        db.query(PsychologicalReport).delete()
        db.query(CheckIn).delete()
        db.query(ActionPlanItem).delete()
        db.query(ActionPlan).delete()
        db.query(ChatSession).delete()
        db.commit()
    finally:
        db.close()

def _make_plan(user_id=1, age_hours=0):
    db = _TestSession()
    try:
        svc = ActionPlanService(db, ai=None)
        plan = svc.generate_plan(user_id, None, "summary")
        if age_hours:
            plan.created_at = datetime.utcnow() - timedelta(hours=age_hours)
            db.commit()
        return plan.id
    finally:
        db.close()


def _make_plan_with_session(user_id=1):
    """Plan linked to a real chat session, so escalation can attach a review."""
    db = _TestSession()
    try:
        session = ChatSession(public_id=f"sess-{user_id}-checkin", title="t", user_id=user_id)
        db.add(session)
        db.commit()
        plan = ActionPlanService(db, ai=None).generate_plan(user_id, session.id, "summary")
        return plan.id
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Pending plans
# ---------------------------------------------------------------------------

def test_pending_plans_empty():
    _clean()
    db = _TestSession()
    try:
        assert len(CheckInService(db).get_pending_plans(1)) == 0
    finally:
        db.close()

def test_pending_plans_found():
    _clean()
    plan_id = _make_plan(age_hours=25)
    db = _TestSession()
    try:
        plans = CheckInService(db).get_pending_plans(1)
        assert len(plans) == 1
        assert plans[0].id == plan_id
    finally:
        db.close()

def test_new_plan_is_available_for_feedback_before_target_window():
    _clean()
    plan_id = _make_plan()
    db = _TestSession()
    try:
        pending = CheckInService(db).get_pending_plans(1)
        assert [plan.id for plan in pending] == [plan_id]
    finally:
        db.close()

def test_pending_excludes_plans_with_checkin():
    _clean()
    plan_id = _make_plan()
    db = _TestSession()
    try:
        svc = CheckInService(db)
        svc.submit_checkin(1, plan_id, "improved")
        assert len(svc.get_pending_plans(1)) == 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Submit check-in
# ---------------------------------------------------------------------------

def test_submit_improved_completes_plan():
    _clean()
    plan_id = _make_plan()
    db = _TestSession()
    try:
        svc = CheckInService(db)
        checkin = svc.submit_checkin(1, plan_id, "improved", "feeling better")
        assert checkin.improvement_status == "improved"
        assert checkin.notes == "feeling better"
        plan = db.get(ActionPlan, plan_id)
        assert plan.status == "completed"
    finally:
        db.close()

def test_submit_unchanged_keeps_active():
    _clean()
    plan_id = _make_plan()
    db = _TestSession()
    try:
        svc = CheckInService(db)
        svc.submit_checkin(1, plan_id, "unchanged")
        plan = db.get(ActionPlan, plan_id)
        assert plan.status == "active"
    finally:
        db.close()

def test_submit_worsened_keeps_active():
    _clean()
    plan_id = _make_plan()
    db = _TestSession()
    try:
        svc = CheckInService(db)
        svc.submit_checkin(1, plan_id, "worsened")
        plan = db.get(ActionPlan, plan_id)
        assert plan.status == "active"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Idempotent submission
# ---------------------------------------------------------------------------

def test_idempotent_update():
    _clean()
    plan_id = _make_plan()
    db = _TestSession()
    try:
        svc = CheckInService(db)
        c1 = svc.submit_checkin(1, plan_id, "unchanged", "first")
        c2 = svc.submit_checkin(1, plan_id, "improved", "second")
        assert c1.id == c2.id  # same check-in, updated
        assert c2.improvement_status == "improved"
        assert c2.notes == "second"
        # Only one check-in exists
        assert len(svc.list_checkins(1)) == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# User isolation
# ---------------------------------------------------------------------------

def test_user_isolation():
    _clean()
    plan_id = _make_plan(1)
    db = _TestSession()
    try:
        svc = CheckInService(db)
        try:
            svc.submit_checkin(2, plan_id, "improved")
            assert False, "Should have raised"
        except ValueError:
            pass
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_invalid_status_raises():
    _clean()
    plan_id = _make_plan()
    db = _TestSession()
    try:
        try:
            CheckInService(db).submit_checkin(1, plan_id, "invalid")
            assert False, "Should have raised"
        except ValueError:
            pass
    finally:
        db.close()


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

def test_api_pending():
    _clean()
    _make_plan(age_hours=25)
    t = _token()
    r = client.get("/api/check-ins/pending", headers=_auth(t))
    assert r.status_code == 200
    assert len(r.json()) == 1

def test_api_submit():
    _clean()
    plan_id = _make_plan()
    t = _token()
    r = client.post("/api/check-ins", json={
        "planId": plan_id, "improvementStatus": "improved", "notes": "better"
    }, headers=_auth(t))
    assert r.status_code == 200
    assert r.json()["improvementStatus"] == "improved"

def test_api_list():
    _clean()
    plan_id = _make_plan()
    t = _token()
    client.post("/api/check-ins", json={"planId": plan_id, "improvementStatus": "improved"}, headers=_auth(t))
    r = client.get("/api/check-ins", headers=_auth(t))
    assert r.status_code == 200
    assert len(r.json()) == 1

def test_api_requires_auth():
    r = client.get("/api/check-ins")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Closed-loop wiring: check-in -> escalation -> human review
# ---------------------------------------------------------------------------

def test_api_checkin_worsened_creates_review():
    """Worsened check-in on a session-linked plan -> SUSTAINED_NO_IMPROVEMENT
    review is created and the student gets the safety message back."""
    _clean()
    plan_id = _make_plan_with_session()
    r = client.post(
        "/api/check-ins",
        json={"planId": plan_id, "improvementStatus": "worsened", "notes": "越来越撑不住"},
        headers=_auth(_token()),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["escalated"] is True
    assert "400-161-9995" in body["safetyMessage"]
    db = _TestSession()
    try:
        reviews = db.query(ReviewRequest).all()
        assert len(reviews) == 1
        assert reviews[0].handoff_reason == "SUSTAINED_NO_IMPROVEMENT"
        assert reviews[0].status == "pending"
        assert "没有改善" not in reviews[0].desensitized_summary  # worsened, not unchanged
        assert "情况恶化" in reviews[0].desensitized_summary
    finally:
        db.close()

def test_api_checkin_improved_no_escalation():
    _clean()
    plan_id = _make_plan_with_session()
    r = client.post(
        "/api/check-ins",
        json={"planId": plan_id, "improvementStatus": "improved"},
        headers=_auth(_token()),
    )
    assert r.status_code == 200, r.text
    assert r.json()["escalated"] is False
    db = _TestSession()
    try:
        assert db.query(ReviewRequest).count() == 0
    finally:
        db.close()

def test_api_checkin_resubmit_does_not_escalate_twice():
    """Idempotent resubmission updates the check-in but must not create a
    second review."""
    _clean()
    plan_id = _make_plan_with_session()
    token = _token()
    for _ in range(2):
        r = client.post(
            "/api/check-ins",
            json={"planId": plan_id, "improvementStatus": "worsened"},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
    assert r.json()["escalated"] is False  # second submission
    db = _TestSession()
    try:
        assert db.query(ReviewRequest).count() == 1
    finally:
        db.close()

def test_api_checkin_without_session_escalates_without_review():
    """Session-less plan (legacy data): escalation is skipped gracefully,
    submission still succeeds."""
    _clean()
    plan_id = _make_plan()
    r = client.post(
        "/api/check-ins",
        json={"planId": plan_id, "improvementStatus": "worsened"},
        headers=_auth(_token()),
    )
    assert r.status_code == 200, r.text
    assert r.json()["escalated"] is False
    db = _TestSession()
    try:
        assert db.query(ReviewRequest).count() == 0
    finally:
        db.close()


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
