from app.api.routes import router
from app.core.config import Settings
from app.core.security import hash_password
from app.models.entities import ChatSession, ReviewRequest, SafetyAssessmentRecord, UserAccount


def test_waiting_review_rejects_new_chat_before_changing_saved_conversation(api_harness_factory, monkeypatch):
    settings = Settings(
        ai_provider="mock", langgraph_checkpoint_backend="memory", knowledge_vector_enabled=False,
        redis_url="redis://127.0.0.1:1/0",
    )
    monkeypatch.setattr("app.api.support.get_settings", lambda: settings)
    harness = api_harness_factory(router)
    with harness.sessions() as db:
        user = UserAccount(username="review-wait", display_name="用户", password_hash=hash_password("secret"))
        user.roles = {"ROLE_USER"}
        db.add(user)
        db.flush()
        session = ChatSession(public_id="waiting-review", title="支持", user_id=user.id)
        db.add(session)
        db.flush()
        report = SafetyAssessmentRecord(
            user_id=user.id, session_id=session.id, content="需要支持", intent="RISK",
            emotion="ANXIETY", emotion_score=0.8, risk_level="HIGH", confidence=0.9, summary="审核",
        )
        db.add(report)
        db.flush()
        db.add(ReviewRequest(session_id=session.id, report_id=report.id, thread_id=session.public_id))
        db.commit()
    token = harness.token("review-wait", "secret")
    response = harness.client.post(
        "/api/chat/stream", json={"sessionId": "waiting-review", "message": "请解释 Python"},
        headers=harness.auth(token),
    )
    assert response.status_code == 409
    conversation = harness.client.get("/api/sessions/waiting-review", headers=harness.auth(token))
    assert conversation.json()["pendingReview"] is True
    assert conversation.json()["messages"] == []
