"""API behavior for executable human-review outcomes (issue 5)."""
from __future__ import annotations

from app.api.routes import router
from app.core.security import hash_password
from app.core.time import utc_now
from app.models.entities import ChatMessage, ChatSession, PsychologicalReport, ReviewRequest, UserAccount
from tests.support import ApiHarness

_harness = ApiHarness(router)
Session = _harness.sessions
client = _harness.client


def _seed_review() -> int:
    db = Session()
    db.query(ReviewRequest).delete()
    db.query(PsychologicalReport).delete()
    db.query(ChatMessage).delete()
    db.query(ChatSession).delete()
    db.query(UserAccount).delete()
    db.commit()
    admin = UserAccount(username="review-admin", display_name="管理员", password_hash=hash_password("admin123"))
    admin.roles = {"ROLE_ADMIN", "ROLE_USER"}
    student = UserAccount(username="review-student", display_name="学生", password_hash=hash_password("student123"))
    student.roles = {"ROLE_USER"}
    db.add_all([admin, student])
    db.flush()
    session = ChatSession(public_id="review-action-session", title="安全支持", user_id=student.id)
    db.add(session)
    db.flush()
    report = PsychologicalReport(
        user_id=student.id,
        session_id=session.id,
        content="需要更多支持",
        intent="RISK",
        emotion="HIGH_RISK",
        emotion_score=4.0,
        risk_level="HIGH",
        confidence=0.95,
        summary="需要人工审核",
    )
    db.add(report)
    db.flush()
    review = ReviewRequest(
        session_id=session.id,
        report_id=report.id,
        thread_id=session.public_id,
        handoff_reason="USER_REQUEST",
        desensitized_summary="当前困境：需要更多支持",
        status="pending",
        created_at=utc_now(),
    )
    db.add(review)
    db.commit()
    review_id = review.id
    db.close()
    return review_id


def _admin_token() -> str:
    response = client.post("/api/auth/login", json={"username": "review-admin", "password": "admin123"})
    return response.json()["accessToken"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_refer_decision_persists_action_and_student_message():
    review_id = _seed_review()
    response = client.post(
        f"/api/admin/reviews/{review_id}/decision",
        json={
            "decision": "refer",
            "referralTarget": "学校心理中心",
            "nextStep": "今天联系值班老师",
        },
        headers=_auth(_admin_token()),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "referred"
    assert "学校心理中心" in body["studentMessage"]

    db = Session()
    try:
        review = db.get(ReviewRequest, review_id)
        assert review.referral_target == "学校心理中心"
        assert review.next_step == "今天联系值班老师"
        assert db.query(ChatMessage).filter(ChatMessage.session_id == review.session_id).count() == 1
    finally:
        db.close()


def test_monitor_decision_requires_action_fields():
    review_id = _seed_review()
    response = client.post(
        f"/api/admin/reviews/{review_id}/decision",
        json={"decision": "monitor"},
        headers=_auth(_admin_token()),
    )
    assert response.status_code == 400
