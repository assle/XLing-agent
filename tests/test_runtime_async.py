"""Public runtime behavior for the sole LangGraph conversation flow."""
from __future__ import annotations

import asyncio
import json

from app.agents.langgraph_runtime import LangGraphAgentRuntimeService
from app.core.enums import IntentType, RiskLevel
from app.models.entities import ChatSession, UserAccount
from app.schemas.dtos import AiMessage
from app.services.knowledge import SearchResult
from tests.support import FakeKnowledgeStore, FakeMemoryStore, build_runtime


def _setup_runtime():
    return build_runtime(
        LangGraphAgentRuntimeService,
        memory=FakeMemoryStore([
            AiMessage(role="user", content="你好"),
            AiMessage(role="assistant", content="你好呀，我在。"),
        ]),
    )


def _user_session(public_id="test-native-runtime-001"):
    return (
        UserAccount(id=1, display_name="测试用户", roles_csv="ROLE_USER"),
        ChatSession(id=1, public_id=public_id, user_id=1),
    )


def test_langgraph_runtime_chat():
    runtime = _setup_runtime()
    user, session = _user_session("native-chat")
    result = asyncio.run(runtime.run(user, session, "帮我写一段 Python 代码"))
    assert result.intent == IntentType.CHAT
    assert result.risk_level == RiskLevel.LOW
    assert result.response_messages
    assert result.retrieved_knowledge == []
    assert result.assessment is None


def test_deployment_resource_query_uses_knowledge_without_cbt_interview():
    resource = SearchResult(1, "deployment-resources.md", "测试热线 TEST-001，非真实服务。", .9)
    runtime = build_runtime(LangGraphAgentRuntimeService, knowledge=FakeKnowledgeStore([resource]),
                            memory=FakeMemoryStore([AiMessage(role="user", content="你好")]))
    user, session = _user_session("native-resource-query")
    result = asyncio.run(runtime.run(user, session, "请告诉我本部署机构的夜间支持热线号码。"))
    assert result.intent == IntentType.CHAT
    assert result.retrieved_knowledge == [resource]
    assert result.cbt_event is None and result.assessment is None
    assert any(resource.content in message.content for message in result.response_messages)


def test_deployment_resource_question_does_not_bypass_distress_or_safety():
    runtime = _setup_runtime()
    user, session = _user_session("native-resource-distress")
    distress = asyncio.run(runtime.run(user, session, "我最近很焦虑，想预约本部署机构的心理支持。"))
    assert distress.intent == IntentType.CONSULT and distress.assessment is not None
    user, session = _user_session("native-resource-danger")
    danger = asyncio.run(runtime.run(user, session, "我准备自杀，请给我本部署机构的支持热线。"))
    assert danger.intent == IntentType.RISK and danger.pending_review


def test_nonkeyword_danger_in_resource_query_still_reaches_intent_model():
    runtime = _setup_runtime()
    calls = []

    async def semantic_risk(messages):
        calls.append(messages)
        return "RISK"

    runtime.ai.acomplete = semantic_risk
    intent = asyncio.run(runtime._classify("我已经吞了一整瓶安眠药，现在想联系机构热线。", []))
    assert intent == IntentType.RISK
    assert len(calls) == 1


def test_langgraph_runtime_consult():
    runtime = _setup_runtime()
    user, session = _user_session("native-support")
    result = asyncio.run(runtime.run(user, session, "最近压力很大，很焦虑"))
    assert result.intent == IntentType.CONSULT
    assert result.assessment is not None
    assert result.response_messages
    assert result.cbt_event is not None


def test_langgraph_runtime_risk_keyword():
    runtime = _setup_runtime()
    user, session = _user_session("native-risk")
    result = asyncio.run(runtime.run(user, session, "我不想活了"))
    assert result.intent == IntentType.RISK
    assert result.risk_level == RiskLevel.HIGH
    assert result.pending_review
    assert result.response_messages == []


def test_new_turn_resets_native_state_and_business_events():
    """A support turn cannot leak its results into the next daily conversation."""
    runtime = _setup_runtime()
    user, session = _user_session("native-turn-isolation")

    async def scenario():
        support = await runtime.run(user, session, "最近压力很大，很焦虑")
        assert support.assessment is not None
        assert support.cbt_event is not None
        chat = await runtime.run(user, session, "帮我写一段 Python 代码")
        snapshot = await runtime.aget_state(session.public_id)
        return chat, snapshot

    chat, snapshot = asyncio.run(scenario())
    assert chat.assessment is None
    assert chat.retrieved_knowledge == []
    assert chat.cbt_event is None
    assert chat.action_plan_event is None
    assert chat.risk_level == RiskLevel.LOW
    assert not chat.pending_review
    assert "context" not in snapshot.values
    assert snapshot.values["model_input"] == "帮我写一段 Python 代码"
    assert snapshot.values["original_run_id"]
    json.dumps(snapshot.values)
