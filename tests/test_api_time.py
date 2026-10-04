from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.api.routes import router
from app.core.security import create_access_token
from app.core.time import utc_isoformat
from app.models.entities import (
    ActionPlan,
    ActionPlanItem,
    ChatMessage,
    ChatSession,
    CheckIn,
    MemoryCard,
    ReviewRequest,
    SafetyAssessmentRecord,
    ScreeningResult,
    UserAccount,
)
from app.schemas.dtos import (
    ConversationMessageResponse,
    DeadLetterResponse,
    ReportResponse,
    ToolJobResponse,
    ToolRecordResponse,
)

STORED_TIME = datetime(2026, 10, 1, 17, 5)
UTC_TIME = "2026-10-01T17:05:00+00:00"


def test_utc_isoformat_marks_database_time_and_converts_aware_time():
    assert utc_isoformat(STORED_TIME) == UTC_TIME
    assert utc_isoformat(datetime(2026, 10, 2, 1, 5, tzinfo=ZoneInfo("Asia/Shanghai"))) == UTC_TIME
    assert utc_isoformat(datetime(2026, 10, 1, 12, 5, tzinfo=timezone(timedelta(hours=-5)))) == UTC_TIME


@pytest.mark.parametrize("model,fields", [
    (ConversationMessageResponse, {"role": "USER", "content": "虚构消息"}),
    (ReportResponse, {
        "id": 1, "sessionId": "s", "username": "u", "displayName": "U",
        "content": "虚构消息", "intent": "CONSULT", "emotion": "ANXIETY", "emotionScore": 1,
        "riskLevel": "LOW", "confidence": 0.9, "summary": "虚构摘要",
    }),
    (ToolRecordResponse, {"id": 1, "reportId": 1, "status": "SUCCESS", "message": "ok"}),
    (ToolJobResponse, {
        "id": 1, "reportId": 1, "kind": "EXCEL_REPORT", "status": "PENDING",
        "attempts": 0, "maxAttempts": 3, "runAfter": STORED_TIME,
        "lastError": "", "updatedAt": STORED_TIME,
    }),
    (DeadLetterResponse, {"id": 1, "reportId": 1, "kind": "EXCEL_REPORT", "reason": "test", "payload": "{}"}),
])
def test_datetime_dtos_keep_storage_values_and_serialize_explicit_utc(model, fields):
    response = model(createdAt=STORED_TIME, **fields)
    assert response.createdAt == STORED_TIME
    payload = response.model_dump(mode="json")
    for key in {"createdAt", "updatedAt", "runAfter"} & payload.keys():
        assert payload[key] == UTC_TIME


@pytest.fixture
def seeded_api(api_harness_factory):
    harness = api_harness_factory(router)
    with harness.sessions() as db:
        user = UserAccount(username="time-test", display_name="虚构测试", password_hash="unused")
        user.roles = {"ROLE_USER", "ROLE_ADMIN"}
        db.add(user)
        db.flush()
        session = ChatSession(
            user_id=user.id, public_id="time-session", title="虚构会话",
            created_at=STORED_TIME, updated_at=STORED_TIME,
        )
        db.add(session)
        db.flush()
        plan = ActionPlan(user_id=user.id, session_id=session.id, created_at=STORED_TIME)
        report = SafetyAssessmentRecord(
            user_id=user.id, session_id=session.id, content="虚构消息", intent="CONSULT", emotion="ANXIETY",
            emotion_score=1, risk_level="HIGH", confidence=0.9, summary="虚构摘要", created_at=STORED_TIME,
        )
        db.add_all([plan, report])
        db.flush()
        review = ReviewRequest(
            session_id=session.id, report_id=report.id, thread_id=session.public_id, created_at=STORED_TIME,
        )
        db.add_all([
            review,
            ChatMessage(user_id=user.id, session_id=session.id, role="USER", content="虚构消息", created_at=STORED_TIME),
            MemoryCard(user_id=user.id, content="虚构偏好", created_at=STORED_TIME, updated_at=STORED_TIME),
            ScreeningResult(
                user_id=user.id, scale_type="GAD-7", answers_json="[0,0,0,0,0,0,0]",
                total_score=0, severity="极少", created_at=STORED_TIME,
            ),
            ActionPlanItem(plan_id=plan.id, content="虚构行动", order_index=0, completed=True, completed_at=STORED_TIME),
            CheckIn(
                user_id=user.id, plan_id=plan.id, improvement_status="unchanged",
                items_snapshot_json="[]", submitted_at=STORED_TIME,
            ),
        ])
        db.commit()
        headers = harness.auth(create_access_token(user))
        review_id = review.id
    return harness, headers, review_id


@pytest.mark.parametrize("endpoint,time_fields", [
    ("/api/sessions", ["createdAt", "updatedAt"]),
    ("/api/memory-cards", ["createdAt", "updatedAt"]),
    ("/api/screening/results", ["createdAt"]),
    ("/api/check-ins", ["submittedAt"]),
    ("/api/action-plans", ["createdAt"]),
    ("/api/admin/reviews", ["createdAt"]),
    ("/api/admin/reports", ["createdAt"]),
])
def test_browser_facing_api_dates_represent_the_stored_utc_instant(seeded_api, endpoint, time_fields):
    harness, headers, _ = seeded_api
    response = harness.client.get(endpoint, headers=headers)
    assert response.status_code == 200
    row = response.json()[0]
    for field in time_fields:
        assert row[field] == UTC_TIME
        assert datetime.fromisoformat(row[field]).astimezone(ZoneInfo("Asia/Shanghai")) == datetime(
            2026, 10, 2, 1, 5, tzinfo=ZoneInfo("Asia/Shanghai"),
        )
    if endpoint == "/api/action-plans":
        assert row["feedbackDueAt"] == "2026-10-02T17:05:00+00:00"
        assert row["items"][0]["completedAt"] == UTC_TIME


def test_conversation_message_dates_are_utc_and_roles_retain_the_storage_contract(seeded_api):
    harness, headers, _ = seeded_api
    for endpoint in ("/api/sessions/time-session", "/api/admin/conversations/time-session"):
        response = harness.client.get(endpoint, headers=headers)
        assert response.status_code == 200
        assert response.json()["messages"][0] == {"role": "USER", "content": "虚构消息", "createdAt": UTC_TIME}


def test_review_follow_up_offset_round_trips_through_naive_utc_storage(seeded_api):
    harness, headers, review_id = seeded_api
    response = harness.client.post(f"/api/admin/reviews/{review_id}/decision", headers=headers, json={
        "decision": "monitor", "followUpOwner": "虚构负责人", "followUpAt": "2026-10-02T10:00:00+08:00",
    })
    assert response.status_code == 200
    assert "2026-10-02 02:00 UTC" in response.json()["studentMessage"]
    with harness.sessions() as db:
        assert db.get(ReviewRequest, review_id).follow_up_at == datetime(2026, 10, 2, 2)
    row = harness.client.get("/api/admin/reviews?all=true", headers=headers).json()[0]
    assert row["followUpAt"] == "2026-10-02T02:00:00+00:00"
    assert datetime.fromisoformat(row["followUpAt"]).astimezone(ZoneInfo("Asia/Shanghai")) == datetime(
        2026, 10, 2, 10, tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    assert datetime.fromisoformat(row["reviewedAt"]).tzinfo == UTC
