from app.api.routes import router
from app.core.config import Settings
from app.core.security import hash_password
from app.models.entities import ChatSession, ReviewRequest, SafetyAssessmentRecord, UserAccount


def test_waiting_human_support_accepts_follow_up_without_running_ai(api_harness_factory, monkeypatch):
    settings = Settings(
        ai_provider="mock", langgraph_checkpoint_backend="memory", knowledge_vector_enabled=False,
        redis_url="redis://127.0.0.1:1/0",
    )
    monkeypatch.setattr("app.api.support.get_settings", lambda: settings)

    def fail_if_runtime_created(*args, **kwargs):
        raise AssertionError("等待人工接手时不得创建常规模型执行器")

    monkeypatch.setattr("app.services.chat.create_agent_runtime", fail_if_runtime_created)
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
        "/api/chat/stream", json={"sessionId": "waiting-review", "message": "我现在的情况有变化"},
        headers=harness.auth(token),
    )
    assert response.status_code == 200
    assert "event: pending_review" in response.text
    assert "event: error" not in response.text

    conversation = harness.client.get("/api/sessions/waiting-review", headers=harness.auth(token))
    body = conversation.json()
    assert body["pendingReview"] is True
    assert body["supportStatus"] == "pending"
    assert [(message["role"], message["content"]) for message in body["messages"]] == [
        ("USER", "我现在的情况有变化"),
        ("ASSISTANT", "我听到了你，你现在的感受很重要。你的消息已进入专业支持流程，支持人员会查看你补充的情况。"
         "你可以继续留言补充情况。如果你现在处于紧急情况，请立刻联系身边可信任的人、"
         "当地紧急服务或部署方提供的专业支持资源。你不是一个人。"),
    ]
