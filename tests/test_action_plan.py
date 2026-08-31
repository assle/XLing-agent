"""Tests for issue 09: Structured 24h action plan.

Covers:
  - Plan generation (LLM + fallback)
  - Schema validation
  - Get/list plans
  - Mark item completed
  - Replace uncompleted item (completed items can't be replaced)
  - User isolation
  - API endpoints

Run: python -m pytest tests/test_action_plan.py
"""
from __future__ import annotations

import json

from app.api.routes import router
from app.core.security import hash_password
from app.models.entities import ActionPlan, ActionPlanItem, UserAccount
from app.services.action_plan import FALLBACK_ITEMS, ActionPlanService
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

def _clean():
    db = _TestSession()
    try:
        db.query(ActionPlanItem).delete()
        db.query(ActionPlan).delete()
        db.commit()
    finally:
        db.close()


class MockAi:
    def __init__(self, response: str = ""):
        self._response = response
    def complete(self, messages):
        return self._response


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def test_generate_plan_with_llm():
    _clean()
    response = json.dumps({"items": [
        {"content": "写下三个担忧", "order": 0},
        {"content": "做10分钟深呼吸", "order": 1},
        {"content": "完成一个25分钟专注时段", "order": 2},
    ]})
    db = _TestSession()
    try:
        svc = ActionPlanService(db, MockAi(response))
        plan = svc.generate_plan(1, None, "CBT summary")
        assert plan.status == "active"
        assert plan.target_window_hours == 24
        assert len(plan.items) == 3
        assert plan.items[0].content == "写下三个担忧"
    finally:
        db.close()

def test_generate_plan_fallback_no_ai():
    _clean()
    db = _TestSession()
    try:
        svc = ActionPlanService(db, ai=None)
        plan = svc.generate_plan(1, None, "CBT summary")
        assert len(plan.items) == len(FALLBACK_ITEMS)
        assert plan.items[0].content == FALLBACK_ITEMS[0]
    finally:
        db.close()

def test_generate_plan_fallback_invalid_json():
    _clean()
    db = _TestSession()
    try:
        svc = ActionPlanService(db, MockAi("not json"))
        plan = svc.generate_plan(1, None, "CBT summary")
        assert len(plan.items) == len(FALLBACK_ITEMS)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Get / list
# ---------------------------------------------------------------------------

def test_get_plan():
    _clean()
    db = _TestSession()
    try:
        svc = ActionPlanService(db, ai=None)
        plan = svc.generate_plan(1, None, "summary")
        fetched = svc.get_plan(1, plan.id)
        assert fetched is not None
        assert fetched.id == plan.id
    finally:
        db.close()

def test_get_plan_user_isolation():
    _clean()
    db = _TestSession()
    try:
        svc = ActionPlanService(db, ai=None)
        plan = svc.generate_plan(1, None, "summary")
        assert svc.get_plan(2, plan.id) is None
    finally:
        db.close()

def test_list_plans():
    _clean()
    db = _TestSession()
    try:
        svc = ActionPlanService(db, ai=None)
        svc.generate_plan(1, None, "s1")
        svc.generate_plan(1, None, "s2")
        plans = svc.list_plans(1)
        assert len(plans) == 2
    finally:
        db.close()


def test_plan_response_exposes_feedback_due_time():
    _clean()
    db = _TestSession()
    try:
        plan = ActionPlanService(db, ai=None).generate_plan(1, None, "summary")
        response = ActionPlanService(db).to_response(plan)
        assert response["feedbackDueAt"] is not None
        assert response["feedbackAvailable"] is True
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Mark completed / replace
# ---------------------------------------------------------------------------

def test_mark_item_completed():
    _clean()
    db = _TestSession()
    try:
        svc = ActionPlanService(db, ai=None)
        plan = svc.generate_plan(1, None, "summary")
        item_id = plan.items[0].id
        item = svc.mark_item_completed(1, item_id)
        assert item.completed is True
        assert item.completed_at is not None
    finally:
        db.close()

def test_replace_uncompleted_item():
    _clean()
    db = _TestSession()
    try:
        svc = ActionPlanService(db, ai=None)
        plan = svc.generate_plan(1, None, "summary")
        item_id = plan.items[0].id
        item = svc.replace_item(1, item_id, "new content")
        assert item.content == "new content"
    finally:
        db.close()

def test_cannot_replace_completed_item():
    _clean()
    db = _TestSession()
    try:
        svc = ActionPlanService(db, ai=None)
        plan = svc.generate_plan(1, None, "summary")
        item_id = plan.items[0].id
        svc.mark_item_completed(1, item_id)
        result = svc.replace_item(1, item_id, "try to replace")
        assert result is None  # can't replace completed
    finally:
        db.close()


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

def test_api_list_plans_empty():
    _clean()
    t = _token()
    r = client.get("/api/action-plans", headers=_auth(t))
    assert r.status_code == 200
    assert r.json() == []

def test_api_get_plan_not_found():
    _clean()
    t = _token()
    r = client.get("/api/action-plans/999", headers=_auth(t))
    assert r.status_code == 404

def test_api_complete_item():
    _clean()
    db = _TestSession()
    try:
        svc = ActionPlanService(db, ai=None)
        plan = svc.generate_plan(1, None, "summary")
        item_id = plan.items[0].id
    finally:
        db.close()
    t = _token()
    r = client.post(f"/api/action-plans/items/{item_id}/complete", headers=_auth(t))
    assert r.status_code == 200
    assert r.json()["completed"] is True

def test_api_requires_auth():
    r = client.get("/api/action-plans")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
