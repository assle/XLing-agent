"""Object-message behavior through the public chat stream and persisted checkpoints."""
from __future__ import annotations

import json

import pytest

from app.api import account, admin, support
from app.api.routes import router
from app.core import security
from app.core.config import Settings
from app.models.entities import (
    ActionPlan,
    ActionPlanItem,
    ChatMessage,
    ChatSession,
    CheckIn,
    ReviewRequest,
    UserAccount,
)
from app.services.action_plan import ActionPlanService
from app.services.memory import RedisShortTermMemoryStore


class _Redis:
    def __init__(self):
        self.lists = {}
        self.values = {}

    def lrange(self, key, start, end):
        return self.lists.get(key, [])[start:None if end == -1 else end + 1]

    def rpush(self, key, *values):
        self.lists.setdefault(key, []).extend(values)

    def ltrim(self, key, start, end):
        self.lists[key] = self.lrange(key, start, end)

    def expire(self, key, seconds):
        return True

    def pipeline(self):
        return self

    def delete(self, key):
        self.lists.pop(key, None)
        self.values.pop(key, None)

    def execute(self):
        return []

    def set(self, key, value, ex=None):
        self.values[key] = value

    def get(self, key):
        return self.values.get(key)


@pytest.fixture
def object_api(api_harness_factory, tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        ai_provider="mock",
        knowledge_vector_enabled=False,
        langgraph_checkpoint_backend="async_sqlite",
        langgraph_checkpoint_path=str(tmp_path / "checkpoints.db"),
        diagnostic_log_dir=str(tmp_path / "logs"),
        tool_queue_enabled=False,
        bcrypt_rounds=4,
        jwt_secret_key="object-message-test-signing-key-at-least-thirty-two-bytes",
    )
    for module in (account, admin, support, security):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    cache = _Redis()
    monkeypatch.setattr(RedisShortTermMemoryStore, "_connect", lambda self: cache)
    harness = api_harness_factory(router)
    with harness.sessions() as db:
        user = UserAccount(
            username="object-user", display_name="Object User",
            password_hash=security.hash_password("test"),
        )
        user.roles = {"ROLE_USER"}
        reviewer = UserAccount(
            username="object-reviewer", display_name="Reviewer",
            password_hash=security.hash_password("test"),
        )
        reviewer.roles = {"ROLE_USER", "ROLE_ADMIN"}
        db.add_all([user, reviewer])
        db.commit()
    headers = harness.auth(harness.token("object-user", "test"))
    harness.reviewer_headers = harness.auth(harness.token("object-reviewer", "test"))
    try:
        yield harness, settings, headers
    finally:
        harness.client.close()


def _seed_plan(harness, public_id="object-session"):
    with harness.sessions() as db:
        user = db.query(UserAccount).filter_by(username="object-user").one()
        session = ChatSession(public_id=public_id, title="次日反馈", user_id=user.id)
        db.add(session)
        db.flush()
        plan = ActionPlanService(db).generate_plan(user.id, session.id, "test plan")
        item_contents = [item.content for item in plan.items]
        return user.id, session.id, public_id, plan.id, item_contents


def _chat(harness, headers, message, session_id):
    response = harness.client.post(
        "/api/chat/stream",
        json={"sessionId": session_id, "message": message},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    events = []
    for block in response.text.strip().split("\n\n"):
        lines = block.splitlines()
        name = next(line[7:] for line in lines if line.startswith("event: "))
        data = json.loads(next(line[6:] for line in lines if line.startswith("data: ")))
        events.append((name, data))
    return events


def _reply(events):
    return "".join(data.get("content", "") for name, data in events if name == "token")


def test_text_checkin_accepts_plan_object_and_submits_improvement(object_api):
    harness, _, headers = object_api
    user_id, _, session_id, plan_id, item_contents = _seed_plan(harness)

    started = _chat(harness, headers, "我要提交次日反馈", session_id)
    assert [name for name, _ in started] == ["meta", "action_plan", "token", "done"], started
    assert started[1][1]["planId"] == plan_id
    assert "改善" in _reply(started)

    selected = _chat(
        harness, headers,
        {"type": "action_plan", "id": plan_id, "title": "客户端不可信的标题"},
        session_id,
    )
    assert [name for name, _ in selected] == ["meta", "token", "done"]
    assert "改善" in _reply(selected)
    assert "没有变化" in _reply(selected)
    assert "恶化" in _reply(selected)

    submitted = _chat(harness, headers, "有改善", session_id)
    assert [name for name, _ in submitted] == ["meta", "token", "done"]
    assert "已提交" in _reply(submitted)
    with harness.sessions() as db:
        checkin = db.query(CheckIn).filter_by(plan_id=plan_id).one()
        assert checkin.improvement_status == "improved"
        assert db.get(ActionPlan, plan_id).status == "completed"
        assert [item.content for item in db.query(ActionPlanItem).filter_by(plan_id=plan_id).order_by(ActionPlanItem.order_index)] == item_contents
        history = db.query(ChatMessage).filter_by(role="USER", user_id=user_id).order_by(ChatMessage.id).all()
        assert history[-2].content.startswith("选择了 24 小时行动计划")
        assert "客户端不可信的标题" not in history[-2].content


def test_object_without_task_only_saves_focus_and_asks_for_intent(object_api):
    import asyncio

    from app.agents.langgraph_runtime import LangGraphAgentRuntimeService

    harness, settings, headers = object_api
    user_id, _, session_id, plan_id, item_contents = _seed_plan(harness, "focus-session")

    selected = _chat(
        harness, headers,
        {"type": "action_plan", "id": plan_id, "title": "任意标题"},
        session_id,
    )
    assert [name for name, _ in selected] == ["meta", "token", "done"]
    assert "希望" in _reply(selected)
    with harness.sessions() as db:
        assert db.query(CheckIn).count() == 0
        assert db.get(ActionPlan, plan_id).status == "active"
        assert [item.content for item in db.query(ActionPlanItem).filter_by(plan_id=plan_id).order_by(ActionPlanItem.order_index)] == item_contents
        async def read_snapshot():
            runtime = LangGraphAgentRuntimeService(db, settings)
            try:
                return await runtime.aget_state(session_id)
            finally:
                await runtime.aclose()

        snapshot = asyncio.run(read_snapshot())
        assert snapshot.values["focused_object"] == {"type": "action_plan", "id": plan_id}
        assert snapshot.values["checkin_task"] is None


def test_object_during_cbt_preserves_interview_progress(object_api):
    harness, _, headers = object_api
    _, _, session_id, plan_id, item_contents = _seed_plan(harness, "cbt-session")
    started = _chat(harness, headers, "工作加班让我担心", session_id)
    cbt = next(data for name, data in started if name == "cbt")
    assert cbt["active"] is True
    assert 0 < cbt["completedCount"] < 4

    selected = _chat(
        harness, headers,
        {"type": "action_plan", "id": plan_id, "title": "计划"},
        session_id,
    )
    assert "继续当前" in _reply(selected)
    with harness.sessions() as db:
        assert db.query(ActionPlan).count() == 1
        assert [item.content for item in db.query(ActionPlanItem).filter_by(plan_id=plan_id).order_by(ActionPlanItem.order_index)] == item_contents
        assert db.query(CheckIn).count() == 0

    continued = _chat(harness, headers, "还会逃避工作安排", session_id)
    next_cbt = next(data for name, data in continued if name == "cbt")
    assert next_cbt["active"] is True
    assert next_cbt["completedCount"] > cbt["completedCount"]


def test_checkin_slots_resume_from_file_checkpoint_without_replaying_plan(object_api):
    from starlette.testclient import TestClient

    harness, _, headers = object_api
    _, _, session_id, plan_id, _ = _seed_plan(harness, "restart-session")
    _chat(harness, headers, "我要提交次日反馈", session_id)
    selected = _chat(
        harness, headers,
        {"type": "action_plan", "id": plan_id, "title": "计划"},
        session_id,
    )
    assert "改善" in _reply(selected)

    harness.client.close()
    harness.client = TestClient(harness.app)
    submitted = _chat(harness, headers, "有改善", session_id)
    assert [name for name, _ in submitted] == ["meta", "token", "done"]
    assert "已提交" in _reply(submitted)
    with harness.sessions() as db:
        assert db.query(CheckIn).filter_by(plan_id=plan_id).one().improvement_status == "improved"


def test_object_request_does_not_call_models_or_knowledge(object_api, monkeypatch):
    from app.services.ai import AiClient
    from app.services.knowledge import KnowledgeService

    harness, _, headers = object_api
    _, _, session_id, plan_id, _ = _seed_plan(harness, "deterministic-session")
    calls = []

    async def forbidden_complete(*args, **kwargs):
        calls.append("model")
        return "CONSULT"

    async def forbidden_stream(*args, **kwargs):
        calls.append("stream")
        if False:
            yield ""

    def forbidden_retrieve(*args, **kwargs):
        calls.append("knowledge")
        return []

    monkeypatch.setattr(AiClient, "acomplete", forbidden_complete)
    monkeypatch.setattr(AiClient, "stream", forbidden_stream)
    monkeypatch.setattr(KnowledgeService, "retrieve", forbidden_retrieve)
    monkeypatch.setattr(KnowledgeService, "aretrieve", forbidden_complete)

    events = _chat(
        harness, headers,
        {"type": "action_plan", "id": plan_id, "title": "计划", "properties": {"unused": True}},
        session_id,
    )
    assert [name for name, _ in events] == ["meta", "token", "done"]
    assert calls == []


def test_worsened_checkin_escalates_once_and_repeat_updates_same_record(object_api):
    harness, _, headers = object_api
    _, _, session_id, plan_id, _ = _seed_plan(harness, "worsened-session")

    def select_and_submit():
        _chat(harness, headers, "我要提交次日反馈", session_id)
        _chat(
            harness, headers,
            {"type": "action_plan", "id": plan_id, "title": "计划"}, session_id,
        )
        return _chat(harness, headers, "恶化了", session_id)

    first = select_and_submit()
    assert [name for name, _ in first] == ["meta", "pending_review", "done"]
    assert "人工审核" in first[1][1]["content"]
    with harness.sessions() as db:
        original_id = db.query(CheckIn).filter_by(plan_id=plan_id).one().id
        review_id = db.query(ReviewRequest).one().id
        assert db.get(ActionPlan, plan_id).status == "active"

    decided = harness.client.post(
        f"/api/admin/reviews/{review_id}/decision", json={"decision": "reject"},
        headers=harness.reviewer_headers,
    )
    assert decided.status_code == 200, decided.text
    repeated = select_and_submit()
    assert [name for name, _ in repeated] == ["meta", "token", "done"]
    with harness.sessions() as db:
        checkins = db.query(CheckIn).filter_by(plan_id=plan_id).all()
        assert len(checkins) == 1
        assert checkins[0].id == original_id
        assert checkins[0].improvement_status == "worsened"
        assert db.query(ReviewRequest).count() == 1


def test_unchanged_checkin_keeps_plan_active(object_api):
    harness, _, headers = object_api
    _, _, session_id, plan_id, _ = _seed_plan(harness, "unchanged-session")
    _chat(harness, headers, "我要提交次日反馈", session_id)
    _chat(
        harness, headers,
        {"type": "action_plan", "id": plan_id, "title": "计划"}, session_id,
    )
    submitted = _chat(harness, headers, "没有变化", session_id)
    assert [name for name, _ in submitted] == ["meta", "pending_review", "done"]
    with harness.sessions() as db:
        assert db.query(CheckIn).filter_by(plan_id=plan_id).one().improvement_status == "unchanged"
        assert db.get(ActionPlan, plan_id).status == "active"


def test_checkin_state_and_plan_selection_are_isolated_by_session(object_api):
    harness, _, headers = object_api
    _, _, first_session, first_plan, _ = _seed_plan(harness, "isolated-first")
    _, _, second_session, second_plan, _ = _seed_plan(harness, "isolated-second")
    _chat(harness, headers, "我要提交次日反馈", first_session)
    _chat(
        harness, headers,
        {"type": "action_plan", "id": first_plan, "title": "第一计划"}, first_session,
    )

    wrong_session = _chat(
        harness, headers,
        {"type": "action_plan", "id": first_plan, "title": "第一计划"}, second_session,
    )
    assert "无法使用" in _reply(wrong_session)
    second_started = _chat(harness, headers, "我要提交次日反馈", second_session)
    second_event = next(data for name, data in second_started if name == "action_plan")
    assert second_event["planId"] == second_plan
    _chat(
        harness, headers,
        {"type": "action_plan", "id": second_plan, "title": "第二计划"}, second_session,
    )
    _chat(harness, headers, "有改善", second_session)
    with harness.sessions() as db:
        assert db.get(ActionPlan, first_plan).status == "active"
        assert db.get(ActionPlan, second_plan).status == "completed"
        assert db.query(CheckIn).filter_by(plan_id=first_plan).count() == 0


def test_original_text_chat_still_uses_existing_support_flow(object_api):
    harness, _, headers = object_api
    events = _chat(harness, headers, "Python 怎么读取 JSON？", None)
    assert events[0][0] == "meta" and events[-1][0] == "done"
    assert "token" in [name for name, _ in events]
    assert _reply(events).strip()


def test_only_action_plan_objects_are_accepted(object_api):
    harness, _, headers = object_api
    response = harness.client.post(
        "/api/chat/stream",
        json={"message": {"type": "memory_card", "id": 1, "title": "不支持"}},
        headers=headers,
    )
    assert response.status_code == 422
