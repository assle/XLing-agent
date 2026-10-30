from __future__ import annotations

from datetime import timedelta

from app.api.routes import router
from app.core.config import Settings
from app.core.security import hash_password
from app.core.time import utc_now
from app.models.entities import ChatSession, ReviewRequest, SafetyAssessmentRecord, UserAccount
from app.services.review import ReviewService


def _seed_support(api_harness_factory):
    harness = api_harness_factory(router)
    with harness.sessions() as db:
        admin = UserAccount(username="support-admin", display_name="支持人员", password_hash=hash_password("admin123"))
        admin.roles = {"ROLE_ADMIN", "ROLE_USER"}
        user = UserAccount(username="support-user", display_name="用户", password_hash=hash_password("user123"))
        user.roles = {"ROLE_USER"}
        db.add_all([admin, user])
        db.flush()
        session = ChatSession(public_id="human-support-session", title="专业支持", user_id=user.id)
        db.add(session)
        db.flush()
        report = SafetyAssessmentRecord(
            user_id=user.id, session_id=session.id, content="需要专业支持", intent="RISK",
            emotion="HIGH_RISK", emotion_score=4.0, risk_level="HIGH", confidence=0.95,
            summary="需要人工接手",
        )
        db.add(report)
        db.flush()
        review = ReviewRequest(
            session_id=session.id, report_id=report.id, thread_id=session.public_id,
            risk_summary="需要人工接手", desensitized_summary="当前困境：需要专业支持", status="pending",
        )
        db.add(review)
        db.commit()
        review_id = review.id
    return harness, review_id, harness.token("support-admin", "admin123"), harness.token("support-user", "user123")


def test_claim_and_human_reply_are_atomic_and_visible_to_user(api_harness_factory):
    harness, review_id, admin_token, user_token = _seed_support(api_harness_factory)

    forbidden = harness.client.post(
        f"/api/admin/reviews/{review_id}/claim", headers=harness.auth(user_token),
    )
    assert forbidden.status_code == 403

    claimed = harness.client.post(
        f"/api/admin/reviews/{review_id}/claim", headers=harness.auth(admin_token),
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["status"] == "in_progress"
    assert claimed.json()["claimedBy"] == "support-admin"
    assert claimed.json()["claimedAt"]

    duplicate_claim = harness.client.post(
        f"/api/admin/reviews/{review_id}/claim", headers=harness.auth(admin_token),
    )
    assert duplicate_claim.status_code == 409

    reply_payload = {"content": "我是本次负责的支持人员，我会继续跟进。", "clientRequestId": "reply-1"}
    replied = harness.client.post(
        f"/api/admin/reviews/{review_id}/reply", json=reply_payload, headers=harness.auth(admin_token),
    )
    assert replied.status_code == 200, replied.text
    assert replied.json()["status"] == "in_progress"
    message_id = replied.json()["messageId"]

    duplicate_reply = harness.client.post(
        f"/api/admin/reviews/{review_id}/reply", json=reply_payload, headers=harness.auth(admin_token),
    )
    assert duplicate_reply.status_code == 200
    assert duplicate_reply.json()["messageId"] == message_id

    conversation = harness.client.get("/api/sessions/human-support-session", headers=harness.auth(user_token))
    body = conversation.json()
    assert body["supportStatus"] == "in_progress"
    assert body["pendingReview"] is True
    human_messages = [message for message in body["messages"] if message["role"] == "SUPPORT"]
    assert len(human_messages) == 1
    assert human_messages[0]["content"] == reply_payload["content"]
    assert human_messages[0]["senderLabel"] == "支持人员"


def test_offsite_contact_records_required_fields_and_user_next_step(api_harness_factory):
    harness, review_id, admin_token, user_token = _seed_support(api_harness_factory)
    assert harness.client.post(
        f"/api/admin/reviews/{review_id}/claim", headers=harness.auth(admin_token),
    ).status_code == 200

    missing = harness.client.post(
        f"/api/admin/reviews/{review_id}/contact", json={"contactMethod": "phone"},
        headers=harness.auth(admin_token),
    )
    assert missing.status_code == 422

    payload = {
        "contactMethod": "phone",
        "contactedAt": "2026-10-30T10:00:00Z",
        "outcome": "已记录用户自述的当前情况",
        "nextStep": "支持人员将在今天 18:00 前再次联系",
        "clientRequestId": "contact-1",
    }
    response = harness.client.post(
        f"/api/admin/reviews/{review_id}/contact", json=payload, headers=harness.auth(admin_token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "in_progress"
    contact_id = response.json()["contactId"]

    duplicate = harness.client.post(
        f"/api/admin/reviews/{review_id}/contact", json=payload, headers=harness.auth(admin_token),
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["contactId"] == contact_id

    body = harness.client.get("/api/sessions/human-support-session", headers=harness.auth(user_token)).json()
    contact_messages = [message["content"] for message in body["messages"] if message["role"] == "SUPPORT"]
    assert len(contact_messages) == 1
    assert "站外联系记录" in contact_messages[0]
    assert payload["nextStep"] in contact_messages[0]
    assert body["supportStatus"] == "in_progress"


def test_human_support_outcomes_require_accountability_and_never_restore_ai(api_harness_factory):
    cases = [
        (
            {"decision": "monitor"},
            {
                "decision": "monitor", "followUpOwner": "值班支持人员",
                "followUpAt": "2026-10-31T10:00:00Z",
            },
            "monitoring", ["持续关注", "值班支持人员"],
        ),
        (
            {"decision": "refer", "referralTarget": "当地专业机构", "nextStep": "今天预约"},
            {
                "decision": "refer", "referralTarget": "当地专业机构", "nextStep": "今天预约",
                "followUpOwner": "值班支持人员",
            },
            "referred", ["当地专业机构", "今天预约", "值班支持人员"],
        ),
        (
            {"decision": "complete", "note": "已记录本次支持过程"},
            {
                "decision": "complete", "note": "已记录本次支持过程",
                "completionBasis": "用户与支持人员确认了当前处置安排",
            },
            "completed", ["已完成", "已记录本次支持过程", "用户与支持人员确认了当前处置安排"],
        ),
    ]
    for missing_payload, valid_payload, expected_status, expected_text in cases:
        harness, review_id, admin_token, user_token = _seed_support(api_harness_factory)
        assert harness.client.post(
            f"/api/admin/reviews/{review_id}/claim", headers=harness.auth(admin_token),
        ).status_code == 200
        missing = harness.client.post(
            f"/api/admin/reviews/{review_id}/decision", json=missing_payload,
            headers=harness.auth(admin_token),
        )
        assert missing.status_code == 400, (valid_payload["decision"], missing.text)

        old_restore = harness.client.post(
            f"/api/admin/reviews/{review_id}/decision", json={"decision": "approve"},
            headers=harness.auth(admin_token),
        )
        assert old_restore.status_code == 400

        response = harness.client.post(
            f"/api/admin/reviews/{review_id}/decision", json=valid_payload,
            headers=harness.auth(admin_token),
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == expected_status
        duplicate = harness.client.post(
            f"/api/admin/reviews/{review_id}/decision", json=valid_payload,
            headers=harness.auth(admin_token),
        )
        assert duplicate.status_code == 409

        body = harness.client.get("/api/sessions/human-support-session", headers=harness.auth(user_token)).json()
        assert body["supportStatus"] == expected_status
        outcome_messages = [message["content"] for message in body["messages"] if message["role"] == "SUPPORT"]
        assert len(outcome_messages) == 1
        for text in expected_text:
            assert text in outcome_messages[0]


def test_timeout_escalates_without_closing_or_messaging_user(api_harness_factory):
    harness, review_id, admin_token, user_token = _seed_support(api_harness_factory)
    with harness.sessions() as db:
        review = db.get(ReviewRequest, review_id)
        review.created_at = utc_now() - timedelta(minutes=20)
        db.commit()
        settings = Settings(tool_queue_enabled=True, review_timeout_minutes=15)
        assert ReviewService(db, settings).escalate_timed_out() == [review_id]
        assert ReviewService(db, settings).escalate_timed_out() == []

    queue = harness.client.get("/api/admin/reviews?all=true", headers=harness.auth(admin_token)).json()
    item = next(item for item in queue if item["reviewId"] == review_id)
    assert item["status"] == "pending"
    assert item["claimedBy"] is None
    assert item["escalationCount"] == 1
    assert item["lastEscalatedAt"]

    jobs = harness.client.get("/api/admin/tool-jobs", headers=harness.auth(admin_token)).json()
    escalation_jobs = [job for job in jobs if job["kind"] == "ESCALATION_ALERT"]
    assert len(escalation_jobs) == 1
    assert escalation_jobs[0]["status"] == "PENDING"

    body = harness.client.get("/api/sessions/human-support-session", headers=harness.auth(user_token)).json()
    assert body["supportStatus"] == "pending"
    assert body["messages"] == []
