"""高风险图执行终止契约；旧 approve/reject 恢复路径已退休。"""
from __future__ import annotations

import asyncio

from app.agents.langgraph_runtime import LangGraphAgentRuntimeService
from app.models.entities import ChatSession, UserAccount
from app.schemas.dtos import AiMessage
from tests.support import FakeMemoryStore, build_runtime


def _make_runtime() -> LangGraphAgentRuntimeService:
    return build_runtime(
        LangGraphAgentRuntimeService,
        memory=FakeMemoryStore([
            AiMessage(role="user", content="你好"),
            AiMessage(role="assistant", content="你好呀"),
        ]),
    )


def _user_session(public_id: str):
    user = UserAccount(id=1, display_name="测试用户", roles_csv="ROLE_USER")
    session = ChatSession(id=1, public_id=public_id, user_id=1)
    return user, session


def test_high_risk_handoff_ends_graph_without_ai_reply_or_resume_entry():
    runtime = _make_runtime()
    user, session = _user_session("human-support-terminal-001")
    result = asyncio.run(runtime.run(user, session, "我不想活了"))
    snapshot = asyncio.run(runtime.aget_state(session.public_id))

    assert result.pending_review is True
    assert result.response_messages == []
    assert not any("Counselor" in step.agent for step in result.steps)
    assert snapshot.next == ()
    assert snapshot.values["awaiting_review"] is True
    assert not hasattr(runtime, "resume")


def test_high_risk_terminal_state_survives_runtime_reconstruction():
    async def scenario():
        runtime_a = _make_runtime()
        user, session = _user_session("human-support-terminal-restart")
        await runtime_a.run(user, session, "我不想活了")

        runtime_b = _make_runtime()
        snapshot = await runtime_b.aget_state(session.public_id)
        assert snapshot.next == ()
        assert snapshot.values["awaiting_review"] is True
        assert snapshot.values["response_messages"] == []
        assert not hasattr(runtime_b, "resume")

    asyncio.run(scenario())
