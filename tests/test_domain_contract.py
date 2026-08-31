"""Public contracts for the general mental-health support domain."""
from __future__ import annotations

from app.api.routes import router
from app.core.config import Settings
from app.core.security import hash_password
from app.models.entities import ReviewRequest, UserAccount
from tests.support import ApiHarness

_harness = ApiHarness(router)
_session = _harness.sessions
client = _harness.client


def _token() -> str:
    db = _session()
    try:
        if db.query(UserAccount).filter_by(username="domain-user").first() is None:
            user = UserAccount(
                username="domain-user",
                display_name="Domain User",
                password_hash=hash_password("safe-password"),
            )
            user.roles = {"ROLE_USER"}
            db.add(user)
            db.commit()
    finally:
        db.close()
    return client.post("/api/auth/login", json={"username": "domain-user", "password": "safe-password"}).json()["accessToken"]


def test_removed_exam_profile_and_personal_assessment_routes_are_not_public() -> None:
    headers = {"Authorization": f"Bearer {_token()}"}
    assert client.get("/api/profile/exam", headers=headers).status_code == 404
    assert client.get("/api/reports/me", headers=headers).status_code == 404


def test_online_settings_have_one_retrieval_path_and_no_calibration_switches() -> None:
    settings = Settings()
    for name in (
        "knowledge_retriever",
        "chroma_snapshot_dir",
        "chroma_snapshot_keep",
        "risk_calibration_artifact",
        "risk_calibration_required",
        "risk_score_endpoint",
        "risk_score_api_key",
    ):
        assert not hasattr(settings, name)


def test_high_risk_screening_creates_a_review_and_returns_a_stable_safety_message() -> None:
    headers = {"Authorization": f"Bearer {_token()}"}
    response = client.post(
        "/api/screening/PHQ-9/submit",
        json={"answers": [0, 0, 0, 0, 0, 0, 0, 0, 1]},
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["highRiskFlagged"] is True
    assert body["escalated"] is True
    assert "当地紧急服务" in body["safetyMessage"]
    db = _session()
    try:
        assert db.query(ReviewRequest).count() == 1
    finally:
        db.close()
