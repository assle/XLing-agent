"""Tests for issue 05: Voluntary explicit screening (PHQ-9 / GAD-7).

Covers:
  - Scale info retrieval (questions, options, rules, disclaimer)
  - Scoring and severity calculation
  - High-risk answer detection (PHQ-9 Q9 self-harm)
  - Answer validation (count, range)
  - User isolation
  - API endpoints (get scale, submit, list, get result)

Run: python -m pytest tests/test_screening.py
"""
from __future__ import annotations

from app.api.routes import router
from app.core.security import hash_password
from app.models.entities import ScreeningResult, UserAccount
from app.services.screening import ScreeningService, score_to_severity
from tests.support import ApiHarness

_harness = ApiHarness(router)
_TestSession = _harness.sessions
client = _harness.client

def _seed():
    """创建两个普通用户供筛查结果归属测试。

    账户使用可通过当前登录验证的密码格式。
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
    """登录指定测试账户并取凭证。

    默认参数使用第一位用户。
    """
    r = client.post("/api/auth/login", json={"username": u, "password": p})
    return r.json()["accessToken"]

def _auth(t):
    """创建携带测试凭证的请求头。

    身份与权限仍由接口执行验证。
    """
    return {"Authorization": f"Bearer {t}"}

def _clean():
    """清空测试筛查结果并提交。

    保留账户和其他测试基础设施。
    """
    db = _TestSession()
    try:
        db.query(ScreeningResult).delete()
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Scale info
# ---------------------------------------------------------------------------

def test_phq9_scale_info():
    """读取九题量表的公开信息。

    检查题数、四档选项、用途及非诊断说明。
    """
    db = _TestSession()
    try:
        info = ScreeningService(db).get_scale_info("PHQ-9")
        assert len(info["questions"]) == 9
        assert len(info["answerOptions"]) == 4
        assert "抑郁" in info["purpose"]
        assert [option["value"] for option in info["answerOptions"]] == [0, 1, 2, 3]
        assert "相加得到总分" in info["scoringRules"]
        assert "20-27=重度" in info["scoringRules"]
        assert "诊断" not in info["disclaimer"] or "不作诊断" in info["disclaimer"]
    finally:
        db.close()

def test_gad7_scale_info():
    """读取七题量表信息。

    检查题数为七且每题使用四个选项。
    """
    db = _TestSession()
    try:
        info = ScreeningService(db).get_scale_info("GAD-7")
        assert len(info["questions"]) == 7
        assert len(info["answerOptions"]) == 4
        assert [option["value"] for option in info["answerOptions"]] == [0, 1, 2, 3]
        assert "相加得到总分" in info["scoringRules"]
        assert "15-21=重度" in info["scoringRules"]
    finally:
        db.close()

def test_invalid_scale_raises():
    """请求不支持的量表名称。

    要求服务明确抛出 ValueError。
    """
    db = _TestSession()
    try:
        try:
            ScreeningService(db).get_scale_info("INVALID")
            assert False, "Should have raised"
        except ValueError:
            pass
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def test_phq9_minimal_score():
    """检查九题量表三分的程度映射。

    应位于最低程度区间。
    """
    assert score_to_severity("PHQ-9", 3) == "minimal"

def test_phq9_mild_score():
    """检查九题量表七分的程度映射。

    应得到轻度标签。
    """
    assert score_to_severity("PHQ-9", 7) == "mild"

def test_phq9_moderate_score():
    """检查九题量表十二分的程度映射。

    应得到中度标签。
    """
    assert score_to_severity("PHQ-9", 12) == "moderate"

def test_phq9_severe_score():
    """检查九题量表二十五分的程度映射。

    应得到最高程度标签。
    """
    assert score_to_severity("PHQ-9", 25) == "severe"

def test_gad7_moderate_score():
    """检查七题量表十二分的程度映射。

    验证使用对应量表的中度区间。
    """
    assert score_to_severity("GAD-7", 12) == "moderate"


# ---------------------------------------------------------------------------
# Submit + high-risk detection
# ---------------------------------------------------------------------------

def test_submit_phq9_no_high_risk():
    """提交总分三分且特定风险题为零的九题答案。

    检查总分准确且没有立即关注标志。
    """
    _clean()
    db = _TestSession()
    try:
        svc = ScreeningService(db)
        answers = [0, 1, 0, 1, 0, 0, 1, 0, 0]  # Q9=0, no high risk
        result = svc.submit_screening(1, "PHQ-9", answers)
        assert result.total_score == 3
        assert result.high_risk_flagged is False
    finally:
        db.close()

def test_submit_phq9_high_risk_q9():
    """其余答案为零，只让第九题非零。

    检查即使总分低也单独标记需立即关注。
    """
    _clean()
    db = _TestSession()
    try:
        svc = ScreeningService(db)
        answers = [0, 0, 0, 0, 0, 0, 0, 0, 1]  # Q9=1, high risk
        result = svc.submit_screening(1, "PHQ-9", answers)
        assert result.high_risk_flagged is True
    finally:
        db.close()

def test_submit_gad7():
    """提交七题量表的固定答案。

    核对总分、轻度标签且不产生九题量表的特定风险标志。
    """
    _clean()
    db = _TestSession()
    try:
        svc = ScreeningService(db)
        answers = [2, 2, 1, 1, 0, 1, 0]
        result = svc.submit_screening(1, "GAD-7", answers)
        assert result.total_score == 7
        assert result.severity == "mild"
        assert result.high_risk_flagged is False  # GAD-7 has no self-harm question
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 输入与归属校验。
# ---------------------------------------------------------------------------

def test_wrong_answer_count_raises():
    """向九题量表只提交三项答案。

    要求拒绝不完整结构，不用默认值补齐。
    """
    _clean()
    db = _TestSession()
    try:
        svc = ScreeningService(db)
        try:
            svc.submit_screening(1, "PHQ-9", [0, 1, 0])  # only 3 answers
            assert False, "Should have raised"
        except ValueError:
            pass
    finally:
        db.close()

def test_out_of_range_answer_raises():
    """在七题答案中包含四分选项。

    检查服务拒绝超出零到三范围的值。
    """
    _clean()
    db = _TestSession()
    try:
        svc = ScreeningService(db)
        try:
            svc.submit_screening(1, "GAD-7", [0, 1, 2, 3, 4, 0, 0])  # 4 is out of range
            assert False, "Should have raised"
        except ValueError:
            pass
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 不同用户的数据访问隔离。
# ---------------------------------------------------------------------------

def test_user_isolation():
    """分别给两位用户保存不同量表结果。

    检查各自列表独立，第一位用户不能读取第二位结果。
    """
    _clean()
    db = _TestSession()
    try:
        svc = ScreeningService(db)
        svc.submit_screening(1, "PHQ-9", [0]*9)
        svc.submit_screening(2, "GAD-7", [0]*7)
        assert len(svc.list_results(1)) == 1
        assert len(svc.list_results(2)) == 1
        # User 1 can't see user 2's result
        r1 = svc.list_results(1)[0]
        assert svc.get_result(1, r1.id) is not None
        r2 = svc.list_results(2)[0]
        assert svc.get_result(1, r2.id) is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 通过接口验证请求与响应。
# ---------------------------------------------------------------------------

def test_api_get_scale():
    """登录后读取量表接口。

    检查成功返回九个题目及说明字段。
    """
    t = _token()
    r = client.get("/api/screening/PHQ-9", headers=_auth(t))
    assert r.status_code == 200
    data = r.json()
    assert len(data["questions"]) == 9
    assert "disclaimer" in data

def test_api_submit():
    """通过接口提交全零的九题答案。

    检查总分、程度、风险标志及筛查说明。
    """
    _clean()
    t = _token()
    r = client.post("/api/screening/PHQ-9/submit", json={"answers": [0]*9}, headers=_auth(t))
    assert r.status_code == 200
    data = r.json()
    assert data["totalScore"] == 0
    assert data["severity"] == "minimal"
    assert data["highRiskFlagged"] is False
    assert "筛查" in data["disclaimer"] or "诊断" in data["disclaimer"]

def test_api_submit_high_risk():
    """通过接口提交特定风险题最高选项。

    检查响应中保留需立即关注标志。
    """
    _clean()
    t = _token()
    r = client.post("/api/screening/PHQ-9/submit", json={"answers": [0,0,0,0,0,0,0,0,3]}, headers=_auth(t))
    assert r.status_code == 200
    assert r.json()["highRiskFlagged"] is True

def test_api_list_results():
    """经接口分别提交两种量表，再查询历史。

    检查当前用户能看到两条保存结果。
    """
    _clean()
    t = _token()
    client.post("/api/screening/PHQ-9/submit", json={"answers": [0]*9}, headers=_auth(t))
    client.post("/api/screening/GAD-7/submit", json={"answers": [0]*7}, headers=_auth(t))
    r = client.get("/api/screening/results", headers=_auth(t))
    assert r.status_code == 200
    assert len(r.json()) == 2

def test_api_requires_auth():
    """未登录请求筛查量表接口。

    检查该接口要求身份验证。
    """
    r = client.get("/api/screening/PHQ-9")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 本组测试结束。
# ---------------------------------------------------------------------------
