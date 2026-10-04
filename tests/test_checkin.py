"""Tests for issue 10: Next-day check-in.

Covers:
  - Pending plans detection
  - Submit check-in (improved/unchanged/worsened)
  - Idempotent submission (update existing)
  - Plan status update based on check-in result
  - User isolation
  - API endpoints

Run: python -m pytest tests/test_checkin.py
"""
from __future__ import annotations

from datetime import timedelta

from app.api.routes import router
from app.core.security import hash_password
from app.core.time import utc_now
from app.models.entities import (
    ActionPlan,
    ActionPlanItem,
    ChatSession,
    CheckIn,
    ReviewRequest,
    SafetyAssessmentRecord,
    UserAccount,
)
from app.services.action_plan import ActionPlanService
from app.services.checkin import CheckInService
from tests.support import ApiHarness

_harness = ApiHarness(router)
_TestSession = _harness.sessions
client = _harness.client

def _seed():
    """准备两个普通用户，供反馈归属测试分别登录。

    提交账户后关闭测试连接。
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
    """用指定测试账号登录并取得凭证。

    默认使用第一位普通用户。
    """
    r = client.post("/api/auth/login", json={"username": u, "password": p})
    return r.json()["accessToken"]

def _auth(t):
    """把给定凭证包装成接口请求头。

    供同一测试中多次请求复用。
    """
    return {"Authorization": f"Bearer {t}"}

def _clean():
    """按依赖顺序清空审核、评估、反馈、行动项、计划和会话。

    保留账户，使每例从干净的支持过程开始。
    """
    db = _TestSession()
    try:
        db.query(ReviewRequest).delete()
        db.query(SafetyAssessmentRecord).delete()
        db.query(CheckIn).delete()
        db.query(ActionPlanItem).delete()
        db.query(ActionPlan).delete()
        db.query(ChatSession).delete()
        db.commit()
    finally:
        db.close()

def _make_plan(user_id=1, age_hours=0):
    """创建不关联会话的默认计划，可把创建时间调早。

    返回编号，用于验证新旧计划的反馈可用性。
    """
    db = _TestSession()
    try:
        svc = ActionPlanService(db, ai=None)
        plan = svc.generate_plan(user_id, None, "summary")
        if age_hours:
            plan.created_at = utc_now() - timedelta(hours=age_hours)
            db.commit()
        return plan.id
    finally:
        db.close()


def _make_plan_with_session(user_id=1):
    """先保存一个用户会话，再生成关联行动计划。

    为需要创建人工审核的反馈测试提供有效会话依据。
    """
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
# 尚未提交反馈的计划。
# ---------------------------------------------------------------------------

def test_pending_plans_empty():
    """清空计划后查询待反馈列表。

    检查没有凭空生成反馈任务。
    """
    _clean()
    db = _TestSession()
    try:
        assert len(CheckInService(db).get_pending_plans(1)) == 0
    finally:
        db.close()

def test_pending_plans_found():
    """准备创建已超过一天的计划。

    检查它出现在待反馈列表并保持相同编号。
    """
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
    """创建新计划后立即查询待反馈列表。

    检查建议反馈时间尚未到也允许提前提交。
    """
    _clean()
    plan_id = _make_plan()
    db = _TestSession()
    try:
        pending = CheckInService(db).get_pending_plans(1)
        assert [plan.id for plan in pending] == [plan_id]
    finally:
        db.close()

def test_pending_excludes_plans_with_checkin():
    """为计划提交改善反馈后再次查询。

    检查已反馈计划不再出现在待反馈列表。
    """
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
# 保存次日反馈并更新计划状态。
# ---------------------------------------------------------------------------

def test_submit_improved_completes_plan():
    """提交改善状态和备注。

    核对反馈内容已保存，并将计划标记完成。
    """
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
    """提交没有改善的反馈。

    检查计划继续保持进行中，供后续调整。
    """
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
    """提交恶化反馈。

    检查计划仍保持进行中，安全升级由其他入口处理。
    """
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
# 同一计划重复提交更新原反馈，不新增重复记录。
# ---------------------------------------------------------------------------

def test_idempotent_update():
    """对同一计划先提交未改善，再提交改善和新备注。

    检查沿用同一反馈编号、更新内容且列表仍只有一条。
    """
    _clean()
    plan_id = _make_plan()
    db = _TestSession()
    try:
        svc = CheckInService(db)
        c1 = svc.submit_checkin(1, plan_id, "unchanged", "first")
        c2 = svc.submit_checkin(1, plan_id, "improved", "second")
        assert c1.id == c2.id  # 仍为同一反馈编号，内容已更新。
        assert c2.improvement_status == "improved"
        assert c2.notes == "second"
        # 验证没有生成第二条反馈。
        assert len(svc.list_checkins(1)) == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 不同用户的数据访问隔离。
# ---------------------------------------------------------------------------

def test_user_isolation():
    """让另一用户给不属于自己的计划提交反馈。

    要求抛出归属校验错误。
    """
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
# 输入与归属校验。
# ---------------------------------------------------------------------------

def test_invalid_status_raises():
    """向自有计划提交不支持的改善状态。

    要求抛出 ValueError，避免保存未知状态。
    """
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
# 通过接口验证请求与响应。
# ---------------------------------------------------------------------------

def test_api_pending():
    """准备一份待反馈计划后通过登录接口查询。

    检查请求成功且返回一份计划。
    """
    _clean()
    _make_plan(age_hours=25)
    t = _token()
    r = client.get("/api/check-ins/pending", headers=_auth(t))
    assert r.status_code == 200
    assert len(r.json()) == 1

def test_api_submit():
    """通过接口提交改善反馈及备注。

    检查成功响应保留提交的改善状态。
    """
    _clean()
    plan_id = _make_plan()
    t = _token()
    r = client.post("/api/check-ins", json={
        "planId": plan_id, "improvementStatus": "improved", "notes": "better"
    }, headers=_auth(t))
    assert r.status_code == 200
    assert r.json()["improvementStatus"] == "improved"

def test_api_list():
    """先经接口提交反馈再查询历史。

    检查反馈确实进入当前用户列表。
    """
    _clean()
    plan_id = _make_plan()
    t = _token()
    client.post("/api/check-ins", json={"planId": plan_id, "improvementStatus": "improved"}, headers=_auth(t))
    r = client.get("/api/check-ins", headers=_auth(t))
    assert r.status_code == 200
    assert len(r.json()) == 1

def test_api_requires_auth():
    """未登录查询反馈历史。

    检查返回 401 而不是暴露已有记录。
    """
    r = client.get("/api/check-ins")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 次日反馈与安全升级、人工审核之间的连接。
# ---------------------------------------------------------------------------

def test_api_checkin_worsened_creates_review():
    """为有会话的计划提交恶化反馈。

    检查响应提供安全提示，并在数据库中生成一条原因正确的待审核记录。
    """
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
    assert "当地紧急服务" in body["safetyMessage"]
    db = _TestSession()
    try:
        reviews = db.query(ReviewRequest).all()
        assert len(reviews) == 1
        assert reviews[0].handoff_reason == "SUSTAINED_NO_IMPROVEMENT"
        assert reviews[0].status == "pending"
        assert "没有改善" not in reviews[0].desensitized_summary  # 本例应记录恶化，而非没有改善。
        assert "情况恶化" in reviews[0].desensitized_summary
    finally:
        db.close()

def test_api_checkin_improved_no_escalation():
    """为有会话的计划提交改善反馈。

    检查没有安全升级标志，也没有新增审核记录。
    """
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
    """连续两次为同一计划提交恶化反馈。

    检查只产生一条审核记录，第二次响应不再次宣称新增升级。
    """
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
    assert r.json()["escalated"] is False  # 第二次提交不重复创建审核。
    db = _TestSession()
    try:
        assert db.query(ReviewRequest).count() == 1
    finally:
        db.close()

def test_api_checkin_without_session_escalates_without_review():
    """对没有关联会话的计划提交恶化反馈。

    当前断言要求接口 escalated 为 False 且无审核记录，避免把内部升级意图当作已成功入队。
    """
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


# 本组测试结束。
# ---------------------------------------------------------------------------
