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
from app.models.entities import ActionPlan, ActionPlanItem, ChatSession, UserAccount
from app.services.action_plan import FALLBACK_ITEMS, ActionPlanService
from tests.support import ApiHarness

_harness = ApiHarness(router)
_TestSession = _harness.sessions
client = _harness.client

def _seed():
    """建立两个不同的普通用户供计划归属测试。

    账户使用当前密码格式，保存后关闭连接。
    """
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
    """通过指定账号密码登录并取出凭证。

    默认返回第一位测试用户的凭证。
    """
    r = client.post("/api/auth/login", json={"username": u, "password": p})
    return r.json()["accessToken"]

def _auth(t):
    """为请求生成携带给定凭证的请求头。

    仅做格式包装，不绕过服务端验证。
    """
    return {"Authorization": f"Bearer {t}"}

def _clean():
    """先删除行动项再删除计划并提交。

    保留测试账户，为每例恢复无计划的状态。
    """
    db = _TestSession()
    try:
        db.query(ActionPlanItem).delete()
        db.query(ActionPlan).delete()
        db.commit()
    finally:
        db.close()


class MockAi:
    def __init__(self, response: str = ""):
        """保存预设的行动计划回复。

        测试可据此提供合法条目或非法文本。
        """
        self._response = response
        self.messages = []
    def complete(self, messages):
        """返回预设模型文字，不进行实际生成。

        将解析与保存行为和外部模型隔离。
        """
        self.messages = messages
        return self._response


# ---------------------------------------------------------------------------
# 行动计划生成。
# ---------------------------------------------------------------------------

def test_generate_plan_with_llm():
    """让模型返回三个有序行动项并生成计划。

    检查计划状态、24 小时窗口和条目正文。
    """
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
    """不提供模型客户端生成计划。

    检查使用预设备用行动项并能保存。
    """
    _clean()
    db = _TestSession()
    try:
        svc = ActionPlanService(db, ai=None)
        plan = svc.generate_plan(1, None, "CBT summary")
        assert len(plan.items) == len(FALLBACK_ITEMS)
        assert plan.items[0].content == FALLBACK_ITEMS[0]
    finally:
        db.close()


def test_generate_plan_preserves_user_constraints_beyond_the_four_dimensions():
    """时间与工作约束不属于四维字段，但必须传给真正生成行动项的模型调用。"""
    _clean()
    ai = MockAi(json.dumps({"items": [{"content": "用两分钟记录担忧，保留工作通知", "order": 0}]}))
    db = _TestSession()
    try:
        context = "工作通知必须保留，我现在只能投入十分钟，不方便外出。"
        plan = ActionPlanService(db, ai).generate_plan(1, None, "加班；担心遗漏；肩颈紧；反复看手机", user_context=context)
        assert context in ai.messages[-1].content
        assert "最新的纠正优先" in ai.messages[0].content
        assert plan.items[0].content == "用两分钟记录担忧，保留工作通知"
    finally:
        db.close()

def test_generate_plan_fallback_invalid_json():
    """让模型返回不可解析文本。

    检查生成流程改用默认行动项，而不是保存坏结构。
    """
    _clean()
    db = _TestSession()
    try:
        svc = ActionPlanService(db, MockAi("not json"))
        plan = svc.generate_plan(1, None, "CBT summary")
        assert len(plan.items) == len(FALLBACK_ITEMS)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 单条读取与列表查询。
# ---------------------------------------------------------------------------

def test_get_plan():
    """生成计划后以所有者编号读取。

    检查返回同一计划编号。
    """
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
    """第一位用户生成计划，第二位用户尝试读取。

    检查服务返回 None，防止跨用户访问。
    """
    _clean()
    db = _TestSession()
    try:
        svc = ActionPlanService(db, ai=None)
        plan = svc.generate_plan(1, None, "summary")
        assert svc.get_plan(2, plan.id) is None
    finally:
        db.close()

def test_list_plans():
    """连续为同一用户生成两份计划。

    检查列表能返回两份记录。
    """
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


def test_session_plan_uses_latest_record_including_completed_and_checks_owner():
    """旧数据有重复计划时，复用最新记录；反馈完成后也不退回较旧计划。"""
    _clean()
    db = _TestSession()
    try:
        session = ChatSession(public_id="latest-plan", title="支持", user_id=1)
        db.add(session)
        db.commit()
        service = ActionPlanService(db)
        older = service.generate_plan(1, session.id, "旧计划")
        latest = service.generate_plan(1, session.id, "新计划")
        # 同一时间的两份旧记录也必须具有确定的新旧顺序。
        latest.created_at = older.created_at
        latest.status = "completed"
        db.commit()
        assert service.get_session_plan(1, session.id).id == latest.id
        assert service.get_session_plan(2, session.id) is None
        assert service.get_session_plan(1, session.id + 1) is None
        assert [plan.id for plan in service.list_plans(1)] == [latest.id, older.id]
    finally:
        db.close()


def test_plan_response_exposes_feedback_due_time():
    """将新计划转换成接口响应。

    检查建议反馈时间存在且反馈入口可用。
    """
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
# 标记完成与替换行动内容。
# ---------------------------------------------------------------------------

def test_mark_item_completed():
    """生成计划后标记第一项完成。

    检查完成标志和完成时间都已保存。
    """
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
    """为尚未完成的行动项提交新内容。

    检查保存后返回修改文本。
    """
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
    """先完成行动项再尝试替换。

    检查返回 None，保留已完成内容。
    """
    _clean()
    db = _TestSession()
    try:
        svc = ActionPlanService(db, ai=None)
        plan = svc.generate_plan(1, None, "summary")
        item_id = plan.items[0].id
        svc.mark_item_completed(1, item_id)
        result = svc.replace_item(1, item_id, "try to replace")
        assert result is None  # 已完成项不能替换。
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 通过接口验证请求与响应。
# ---------------------------------------------------------------------------

def test_api_list_plans_empty():
    """清空计划后带合法凭证查询列表。

    检查成功返回空数组。
    """
    _clean()
    t = _token()
    r = client.get("/api/action-plans", headers=_auth(t))
    assert r.status_code == 200
    assert r.json() == []


def test_api_session_filter_restores_older_active_plan_without_other_thread_or_user_data():
    """较新的其他会话已完成计划，不能隐藏当前会话仍 active 的计划。"""
    _clean()
    db = _TestSession()
    try:
        first_session = ChatSession(public_id="api-plan-first", title="第一会话", user_id=1)
        second_session = ChatSession(public_id="api-plan-second", title="第二会话", user_id=1)
        other_session = ChatSession(public_id="api-plan-other-owner", title="其他用户", user_id=2)
        db.add_all([first_session, second_session, other_session])
        db.commit()
        plans = ActionPlanService(db)
        first = plans.generate_plan(1, first_session.id, "第一份计划")
        second = plans.generate_plan(1, second_session.id, "第二份计划")
        second.status = "completed"
        plans.generate_plan(2, other_session.id, "其他用户的计划")
        db.commit()
        first_id, second_id = first.id, second.id
    finally:
        db.close()
    headers = _auth(_token())
    first_response = client.get("/api/action-plans", params={"sessionId": "api-plan-first"}, headers=headers)
    assert first_response.status_code == 200
    assert [(plan["id"], plan["status"]) for plan in first_response.json()] == [(first_id, "active")]
    second_response = client.get("/api/action-plans", params={"sessionId": "api-plan-second"}, headers=headers)
    assert [(plan["id"], plan["status"]) for plan in second_response.json()] == [(second_id, "completed")]
    assert client.get("/api/action-plans", params={"sessionId": "api-plan-other-owner"}, headers=headers).json() == []
    assert client.get("/api/action-plans", params={"sessionId": "missing"}, headers=headers).json() == []
    assert {plan["id"] for plan in client.get("/api/action-plans", headers=headers).json()} == {first_id, second_id}

def test_api_get_plan_not_found():
    """带合法凭证读取不存在的计划编号。

    检查接口返回 404。
    """
    _clean()
    t = _token()
    r = client.get("/api/action-plans/999", headers=_auth(t))
    assert r.status_code == 404

def test_api_complete_item():
    """先在数据库准备计划，再通过接口完成一个行动项。

    检查响应成功且 completed 为真。
    """
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
    """不登录请求计划列表。

    检查接口返回 401。
    """
    r = client.get("/api/action-plans")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 本组测试结束。
# ---------------------------------------------------------------------------
