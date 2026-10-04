"""Public runtime behavior for the sole LangGraph conversation flow."""
from __future__ import annotations

import asyncio
import json

from app.agents.langgraph_runtime import LangGraphAgentRuntimeService
from app.core.enums import IntentType, RiskLevel
from app.models.entities import ChatSession, UserAccount
from app.schemas.dtos import AiMessage
from tests.support import FakeMemoryStore, build_runtime


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
