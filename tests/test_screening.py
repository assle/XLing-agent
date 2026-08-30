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
        db.query(ScreeningResult).delete()
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Scale info
# ---------------------------------------------------------------------------

def test_phq9_scale_info():
    db = _TestSession()
    try:
        info = ScreeningService(db).get_scale_info("PHQ-9")
        assert len(info["questions"]) == 9
        assert len(info["answerOptions"]) == 4
        assert "抑郁" in info["purpose"]
        assert "诊断" not in info["disclaimer"] or "不作诊断" in info["disclaimer"]
    finally:
        db.close()

def test_gad7_scale_info():
    db = _TestSession()
    try:
        info = ScreeningService(db).get_scale_info("GAD-7")
        assert len(info["questions"]) == 7
        assert len(info["answerOptions"]) == 4
    finally:
        db.close()

def test_invalid_scale_raises():
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
    assert score_to_severity("PHQ-9", 3) == "minimal"

def test_phq9_mild_score():
    assert score_to_severity("PHQ-9", 7) == "mild"

def test_phq9_moderate_score():
    assert score_to_severity("PHQ-9", 12) == "moderate"

def test_phq9_severe_score():
    assert score_to_severity("PHQ-9", 25) == "severe"

def test_gad7_moderate_score():
    assert score_to_severity("GAD-7", 12) == "moderate"


# ---------------------------------------------------------------------------
# Submit + high-risk detection
# ---------------------------------------------------------------------------

def test_submit_phq9_no_high_risk():
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
# Validation
# ---------------------------------------------------------------------------

def test_wrong_answer_count_raises():
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
# User isolation
# ---------------------------------------------------------------------------

def test_user_isolation():
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
# API endpoints
# ---------------------------------------------------------------------------

def test_api_get_scale():
    t = _token()
    r = client.get("/api/screening/PHQ-9", headers=_auth(t))
    assert r.status_code == 200
    data = r.json()
    assert len(data["questions"]) == 9
    assert "disclaimer" in data

def test_api_submit():
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
    _clean()
    t = _token()
    r = client.post("/api/screening/PHQ-9/submit", json={"answers": [0,0,0,0,0,0,0,0,3]}, headers=_auth(t))
    assert r.status_code == 200
    assert r.json()["highRiskFlagged"] is True

def test_api_list_results():
    _clean()
    t = _token()
    client.post("/api/screening/PHQ-9/submit", json={"answers": [0]*9}, headers=_auth(t))
    client.post("/api/screening/GAD-7/submit", json={"answers": [0]*7}, headers=_auth(t))
    r = client.get("/api/screening/results", headers=_auth(t))
    assert r.status_code == 200
    assert len(r.json()) == 2

def test_api_requires_auth():
    r = client.get("/api/screening/PHQ-9")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
