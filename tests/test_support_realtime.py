from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.api.routes import router
from app.core.security import hash_password
from app.models.entities import ChatSession, ReviewRequest, SafetyAssessmentRecord, UserAccount
from app.services.support_events import SupportEvent, SupportEventBroker, support_events
from app.services.support_turn import SupportTurnTransaction
from tests.support import DatabaseHarness, make_test_settings
from tests.test_support_turn_transaction import _high_risk_run


def _seed_support(api_harness_factory):
    harness = api_harness_factory(router)
    with harness.sessions() as db:
        admin = UserAccount(username="realtime-admin", display_name="支持人员", password_hash=hash_password("admin123"))
        admin.roles = {"ROLE_ADMIN", "ROLE_USER"}
        user = UserAccount(username="realtime-user", display_name="用户", password_hash=hash_password("user123"))
        user.roles = {"ROLE_USER"}
        db.add_all([admin, user])
        db.flush()
        session = ChatSession(public_id="realtime-session", title="专业支持", user_id=user.id)
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
    return harness, review_id, harness.token("realtime-admin", "admin123"), harness.token("realtime-user", "user123")


def test_admin_event_stream_requires_an_authorized_reviewer(api_harness_factory):
    harness, _, _, user_token = _seed_support(api_harness_factory)

    response = harness.client.get("/api/admin/reviews/events", headers=harness.auth(user_token))

    assert response.status_code == 403


async def _next_sse_event(response):
    event_name = None
    async for line in response.aiter_lines():
        if line.startswith("event: "):
            event_name = line[7:]
        elif line.startswith("data: ") and event_name:
            return event_name, json.loads(line[6:])
    raise AssertionError("事件流在收到消息前关闭")


def test_pending_user_message_is_published_to_the_admin_stream(api_harness_factory):
    harness, _, _, user_token = _seed_support(api_harness_factory)

    async def exercise():
        subscription = support_events.subscribe(admin=True)
        try:
            transport = httpx.ASGITransport(app=harness.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/api/chat/stream",
                    json={"sessionId": "realtime-session", "message": "我还想补充一些情况"},
                    headers=harness.auth(user_token),
                )
                async for _ in response.aiter_text():
                    pass
            event = await asyncio.wait_for(subscription.queue.get(), timeout=2)
        finally:
            support_events.unsubscribe(subscription)

        assert event.event_type == "user_message"
        assert event.session_id == "realtime-session"
        assert event.support_status == "pending"
        assert event.content == "我还想补充一些情况"
        assert event.message_id

    asyncio.run(exercise())


def test_human_reply_is_published_only_to_the_owned_session(api_harness_factory):
    harness, review_id, admin_token, _ = _seed_support(api_harness_factory)

    async def exercise():
        subscription = support_events.subscribe(admin=False, session_id="realtime-session")
        try:
            transport = httpx.ASGITransport(app=harness.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                claimed = await client.post(
                    f"/api/admin/reviews/{review_id}/claim", headers=harness.auth(admin_token),
                )
                assert claimed.status_code == 200
                status_event = await asyncio.wait_for(subscription.queue.get(), timeout=2)

                payload = {"content": "我会继续陪你梳理当前的情况。", "clientRequestId": "realtime-reply-1"}
                replied = await client.post(
                    f"/api/admin/reviews/{review_id}/reply", json=payload, headers=harness.auth(admin_token),
                )
                assert replied.status_code == 200
                message_event = await asyncio.wait_for(subscription.queue.get(), timeout=2)

                duplicate = await client.post(
                    f"/api/admin/reviews/{review_id}/reply", json=payload, headers=harness.auth(admin_token),
                )
                assert duplicate.status_code == 200
                await asyncio.sleep(0.05)
                assert subscription.queue.empty()
        finally:
            support_events.unsubscribe(subscription)

        assert status_event.event_type == "status_changed"
        assert status_event.support_status == "in_progress"
        assert message_event.event_type == "support_message"
        assert message_event.role == "SUPPORT"
        assert message_event.content == payload["content"]
        assert message_event.message_id == replied.json()["messageId"]

    asyncio.run(exercise())


def test_new_review_is_published_only_after_the_transaction_commits():
    async def exercise():
        subscription = support_events.subscribe(admin=True)
        harness = DatabaseHarness()
        db = harness.sessions()
        try:
            user = UserAccount(username="new-review-user", display_name="用户", password_hash="hash")
            session = ChatSession(public_id="new-review-session", title="专业支持", user_id=1)
            db.add_all([user, session])
            db.commit()
            SupportTurnTransaction(db, make_test_settings()).save_support_turn(
                user=user,
                session=session,
                content="我不想活了",
                run=_high_risk_run(),
            )
            event = await asyncio.wait_for(subscription.queue.get(), timeout=2)
        finally:
            support_events.unsubscribe(subscription)
            db.close()
            harness.close()

        assert event.event_type == "review_created"
        assert event.session_id == "new-review-session"
        assert event.support_status == "pending"
        assert event.review["status"] == "pending"
        assert event.review["recentContext"][0]["content"] == "我不想活了"

    asyncio.run(exercise())


def test_rolled_back_review_does_not_publish_an_event():
    async def exercise():
        subscription = support_events.subscribe(admin=True)
        harness = DatabaseHarness()
        db = harness.sessions()
        try:
            user = UserAccount(username="rollback-review-user", display_name="用户", password_hash="hash")
            session = ChatSession(public_id="rollback-review-session", title="专业支持", user_id=1)
            db.add_all([user, session])
            db.commit()

            def fail_before_commit(stage):
                if stage == "jobs":
                    raise RuntimeError("synthetic rollback")

            with pytest.raises(RuntimeError, match="synthetic rollback"):
                SupportTurnTransaction(
                    db, make_test_settings(tool_queue_enabled=True), fault_injector=fail_before_commit,
                ).save_support_turn(user=user, session=session, content="我不想活了", run=_high_risk_run())
            await asyncio.sleep(0.05)
            assert subscription.queue.empty()
        finally:
            support_events.unsubscribe(subscription)
            db.close()
            harness.close()

    asyncio.run(exercise())


def test_admin_sse_emits_committed_event_and_disconnects_cleanly(api_harness_factory):
    harness, _, admin_token, _ = _seed_support(api_harness_factory)

    async def exercise():
        disconnect = asyncio.Event()
        response_started = asyncio.Event()
        bodies = []
        request_sent = False

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                response_started.set()
            if message["type"] == "http.response.body":
                body = message.get("body", b"")
                bodies.append(body)
                if b"user_message" in body:
                    disconnect.set()

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"},
            "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": "/api/admin/reviews/events", "raw_path": b"/api/admin/reviews/events",
            "query_string": b"", "root_path": "", "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "headers": [(b"authorization", f"Bearer {admin_token}".encode())],
        }
        request = asyncio.create_task(harness.app(scope, receive, send))
        await asyncio.wait_for(response_started.wait(), timeout=2)
        await asyncio.sleep(0.05)
        support_events.publish(SupportEvent(
            event_type="user_message", review_id=1, session_id="realtime-session",
            support_status="pending", message_id=1, role="USER", content="实时补充",
        ))
        await asyncio.wait_for(request, timeout=2)

        frames = b"".join(bodies).decode()
        assert "event: user_message" in frames
        assert '"content": "实时补充"' in frames

    asyncio.run(exercise())


def test_user_event_stream_requires_session_ownership(api_harness_factory):
    harness, _, _, _ = _seed_support(api_harness_factory)
    with harness.sessions() as db:
        other = UserAccount(username="other-realtime-user", display_name="其他用户", password_hash=hash_password("user123"))
        other.roles = {"ROLE_USER"}
        db.add(other)
        db.commit()
    other_token = harness.token("other-realtime-user", "user123")

    response = harness.client.get("/api/sessions/realtime-session/events", headers=harness.auth(other_token))

    assert response.status_code == 404


def test_slow_subscription_receives_a_resync_signal_and_heartbeat():
    async def exercise():
        broker = SupportEventBroker(queue_size=1)
        subscription = broker.subscribe(admin=True)
        try:
            for message_id in (1, 2):
                broker.publish(SupportEvent(
                    event_type="user_message", review_id=1, session_id="realtime-session",
                    support_status="pending", message_id=message_id,
                ))
            await asyncio.sleep(0.05)
            event = await asyncio.wait_for(subscription.queue.get(), timeout=1)
            assert event.event_type == "resync_required"

            stream = broker.stream(subscription, heartbeat_seconds=0.01)
            heartbeat = await asyncio.wait_for(anext(stream), timeout=1)
            assert heartbeat == ": keep-alive\n\n"
            await stream.aclose()
        finally:
            broker.unsubscribe(subscription)

    asyncio.run(exercise())
