"""Model input boundaries for reply claims, user constraints and retained context.

These tests prove what the runtime supplies to the model; generated reply quality
requires a separate real-provider simulation.
"""
from __future__ import annotations

import asyncio

import pytest

from app.agents.langgraph_runtime import LangGraphAgentRuntimeService
from app.models.entities import ChatSession, MemoryCard, UserAccount, UserProfile
from app.schemas.dtos import AiMessage
from app.services.action_plan import ActionPlanService
from tests.support import FakeMemoryStore, build_runtime


def _seed(db, suffix, *, no_memory=False):
    user = UserAccount(username=f"reply-{suffix}", display_name="测试用户", password_hash="unused")
    db.add(user)
    db.flush()
    session = ChatSession(public_id=f"reply-{suffix}", title="测试", user_id=user.id, no_memory=no_memory)
    db.add(session)
    db.commit()
    return user, session


@pytest.mark.parametrize("text", ["用两句话解释 Python 列表", "我最近工作压力很大", "我不想活了"])
def test_no_memory_fact_is_supplied_to_chat_interview_and_review_reply(database_harness, text):
    db = database_harness.sessions()
    try:
        user, session = _seed(db, text, no_memory=True)
        db.add_all([
            UserProfile(user_id=user.id, current_concern="PRIVATE-PROFILE-CONTENT"),
            MemoryCard(user_id=user.id, content="PRIVATE-CARD-CONTENT", confirmed=True, source="user"),
        ])
        db.commit()
        remembered_message = "本会话里刚才说过的安排"
        runtime = build_runtime(
            LangGraphAgentRuntimeService, db=db,
            memory=FakeMemoryStore([AiMessage(role="user", content=remembered_message)]),
        )
        result = asyncio.run(runtime.run(user, session, text))
        if result.pending_review:
            result = asyncio.run(runtime.resume(session.public_id, approved=True))
        content = "\n".join(message.content for message in result.response_messages)
        assert "本次是无记忆会话" in content
        assert "关闭页面或结束会话不会删除它们" in content
        assert "所需的例子和说明也计入指定句数或段落数" in content
        assert "不要额外补充引言、总结、说明段或追问" in content
        assert "PRIVATE-PROFILE-CONTENT" not in content
        assert "PRIVATE-CARD-CONTENT" not in content
        assert remembered_message in content
        assert db.query(MemoryCard).count() == 1
        assert result.response_messages[-1].role == "user"
        assert result.response_messages[-1].content == text
    finally:
        db.close()


@pytest.mark.parametrize("latest_request", [
    "先不讲路线了，只用两句话和一个生活例子解释列表和字典",
    "先不讲路线了，用一段文字和一个生活例子解释通货膨胀，不要追加总结",
])
def test_new_request_follows_interrupted_history_without_becoming_an_old_assistant_reply(
    database_harness, latest_request,
):
    db = database_harness.sessions()
    try:
        user, session = _seed(db, "latest-request")
        old_request = "给我十个阶段的 Python 学习路线"
        memory = FakeMemoryStore([
            AiMessage(role="user", content=old_request),
            AiMessage(role="assistant", content="第一阶段先学习基础语法，第二阶段"),
        ])
        runtime = build_runtime(LangGraphAgentRuntimeService, db=db, memory=memory)
        result = asyncio.run(runtime.run(user, session, latest_request))
        assert [message.content for message in result.response_messages if message.role == "user"] == [
            old_request, latest_request,
        ]
        assert result.response_messages[-1].content == latest_request
        assert any("不要继续回答已被替换的旧问题" in message.content for message in result.response_messages)
        assert any("未确认在校时不要默认推荐" in message.content for message in result.response_messages)
        contract = next(
            message for message in result.response_messages
            if message.role == "system" and message.content.startswith("会话与产品事实：")
        )
        assert "用户明确指定句数、段落数或格式时，按该总量组织整条回复" in contract.content
    finally:
        db.close()


def test_first_plan_receives_current_and_earlier_constraints_with_profile(database_harness, monkeypatch):
    db = database_harness.sessions()
    captured_contexts = []
    original_generate = ActionPlanService._generate_items

    def capture(service, summary, user_context=""):
        captured_contexts.append(user_context)
        return original_generate(service, summary, user_context)

    monkeypatch.setattr(ActionPlanService, "_generate_items", capture)
    try:
        user, session = _seed(db, "plan-constraints")
        db.add(UserProfile(user_id=user.id, current_concern="物流调度，工作通知必须响"))
        db.commit()
        earlier_constraint = "目前只能用十分钟，不方便离开岗位"
        memory = FakeMemoryStore([AiMessage(role="user", content=earlier_constraint)])
        runtime = build_runtime(LangGraphAgentRuntimeService, db=db, memory=memory)
        latest = "工作加班让我担心，睡不着，还会逃避；请保持工作通知，不改变设备设置"
        result = asyncio.run(runtime.run(user, session, latest))
        assert result.action_plan_event is not None
        assert len(captured_contexts) == 1
        assert earlier_constraint in captured_contexts[0]
        assert latest in captured_contexts[0]
        assert "物流调度，工作通知必须响" in captured_contexts[0]
    finally:
        db.close()
