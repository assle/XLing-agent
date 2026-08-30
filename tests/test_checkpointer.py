"""Tests for LangGraph Checkpointer integration (issue 03).

Verifies that:
- Each run's Agent state is checkpointed under thread_id = session.public_id
- Crash recovery: a new runtime reading the same SqliteSaver file recovers
  the previous run's state
- Different thread_ids have isolated checkpoint state

Uses mock AiClient + fake memory/knowledge stores -- no Redis/MySQL/Chroma.

Run: python -m pytest tests/test_checkpointer.py
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

from app.agents.langgraph_runtime import LangGraphAgentRuntimeService
from app.agents.runtime import ActionPlanEvent, ActionPlanItemEvent, AgentContext, CbtEvent
from app.core.config import Settings
from app.core.enums import EmotionLabel, IntentType, RiskLevel
from app.models.entities import ChatSession, UserAccount
from app.schemas.dtos import AiMessage
from app.services.assessment import PsychologyAssessment
from app.services.knowledge import SearchResult
from tests.support import FakeMemoryStore, build_runtime


def _make_runtime(backend: str, checkpoint_path: str | None = None, checkpointer=None) -> LangGraphAgentRuntimeService:
    kwargs = {"ai_provider": "mock", "langgraph_checkpoint_backend": backend}
    if checkpoint_path:
        kwargs["langgraph_checkpoint_path"] = checkpoint_path
    runtime = build_runtime(
        LangGraphAgentRuntimeService,
        settings=Settings(**kwargs),
        memory=FakeMemoryStore([
            AiMessage(role="user", content="你好"),
            AiMessage(role="assistant", content="你好呀"),
        ]),
    )
    if checkpointer is not None:
        runtime._checkpointer = checkpointer
        runtime.graph = runtime._build_graph()
    return runtime


def _user_session(public_id: str = "session-checkpoint-001"):
    user = UserAccount(id=1, display_name="测试学生", roles_csv="ROLE_USER")
    session = ChatSession(id=1, public_id=public_id, user_id=1)
    return user, session


def test_agent_context_checkpoint_payload_is_json_safe_and_round_trips():
    user, session = _user_session("json-safe-001")
    context = AgentContext(
        user=user,
        session=session,
        original_input="最近很焦虑",
        model_input="最近很焦虑",
        intent=IntentType.CONSULT,
        risk_level=RiskLevel.MEDIUM,
        assessment=PsychologyAssessment(
            EmotionLabel.ANXIETY,
            2.5,
            RiskLevel.MEDIUM,
            0.7,
            "压力表达",
            risk_probabilities={"LOW": 0.2, "MEDIUM": 0.7, "HIGH": 0.1},
            raw_risk_probabilities={"LOW": 0.1, "MEDIUM": 0.8, "HIGH": 0.1},
            prediction_set=(RiskLevel.MEDIUM,),
            model_version="risk-v1",
            calibration_version="cal-v1",
        ),
        retrieved_knowledge=[SearchResult(1, "guide.md", "支持内容", 0.8)],
        cbt_event=CbtEvent(True, 2, "body_reactions", False),
        action_plan_event=ActionPlanEvent(
            9,
            [ActionPlanItemEvent(1, "先休息十分钟", 0, False)],
            "2026-08-31T00:00:00",
        ),
    )

    payload = context.to_checkpoint()
    restored = AgentContext.from_checkpoint(payload)

    json.dumps(payload, ensure_ascii=False)
    assert restored.user.id == user.id
    assert restored.session.public_id == session.public_id
    assert restored.intent == IntentType.CONSULT
    assert restored.assessment.calibration_version == "cal-v1"
    assert restored.assessment.raw_risk_probabilities == {
        "LOW": 0.1,
        "MEDIUM": 0.8,
        "HIGH": 0.1,
    }
    assert restored.retrieved_knowledge[0].source == "guide.md"
    assert restored.action_plan_event.items[0].content == "先休息十分钟"


# ---------------------------------------------------------------------------
# State is checkpointed after a run (MemorySaver)
# ---------------------------------------------------------------------------

def test_state_checkpointed_after_run():
    runtime = _make_runtime("memory")
    user, session = _user_session("session-mem-001")
    asyncio.run(runtime.run(user, session, "帮我写一段 Python 代码", "帮我写一段 Python 代码"))
    snapshot = runtime.get_state("session-mem-001")
    assert snapshot is not None
    context = snapshot.values["context"]
    assert context.intent == IntentType.CHAT


# ---------------------------------------------------------------------------
# Crash recovery: new runtime instance with the SAME checkpointer recovers state
# ---------------------------------------------------------------------------

def test_crash_recovery_shared_checkpointer():
    # A shared MemorySaver simulates a persistent store that survives a crash.
    # Runtime A checkpoints; a fresh Runtime B (same process, new instance)
    # reading the same checkpointer recovers A's state.
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    shared_saver = MemorySaver(serde=JsonPlusSerializer())

    runtime_a = _make_runtime("memory", checkpointer=shared_saver)
    user, session = _user_session("session-crash-001")
    result_a = asyncio.run(runtime_a.run(user, session, "帮我写一段 Python 代码", "帮我写一段 Python 代码"))
    assert result_a.intent == IntentType.CHAT

    # Simulate crash: discard runtime A, create runtime B with same checkpointer
    runtime_b = _make_runtime("memory", checkpointer=shared_saver)
    snapshot = runtime_b.get_state("session-crash-001")
    assert snapshot is not None
    context = snapshot.values["context"]
    assert context.intent == IntentType.CHAT


# ---------------------------------------------------------------------------
# Different thread_ids have independent checkpoint state
# ---------------------------------------------------------------------------

def test_thread_isolation():
    runtime = _make_runtime("memory")
    user1, session1 = _user_session("session-iso-001")
    user2, session2 = _user_session("session-iso-002")
    asyncio.run(runtime.run(user1, session1, "帮我写一段 Python 代码", "帮我写一段 Python 代码"))
    asyncio.run(runtime.run(user2, session2, "我不想活了", "我不想活了"))

    ctx1 = runtime.get_state("session-iso-001").values["context"]
    ctx2 = runtime.get_state("session-iso-002").values["context"]
    assert ctx1.intent == IntentType.CHAT
    assert ctx2.intent == IntentType.RISK


# ---------------------------------------------------------------------------
# Two runs on same thread both checkpoint (history grows)
# ---------------------------------------------------------------------------

def test_same_thread_two_runs():
    runtime = _make_runtime("memory")
    user, session = _user_session("session-twice-001")
    asyncio.run(runtime.run(user, session, "帮我写一段 Python 代码", "帮我写一段 Python 代码"))
    # Second run on same thread -- state is overwritten with the new run's result
    asyncio.run(runtime.run(user, session, "帮我写一段 Java 代码", "帮我写一段 Java 代码"))
    snapshot = runtime.get_state("session-twice-001")
    assert snapshot is not None
    context = snapshot.values["context"]
    assert context.intent == IntentType.CHAT


def test_async_sqlite_checkpoint_resumes_after_runtime_restart(tmp_path):
    async def scenario():
        path = str(tmp_path / "checkpoints.db")
        runtime_a = _make_runtime("async_sqlite", path)
        user, session = _user_session("persistent-approve-001")
        interrupted = await runtime_a.run(user, session, "我不想活了", "我不想活了")
        assert interrupted.pending_review is True
        await runtime_a.aclose()

        runtime_b = _make_runtime("async_sqlite", path)
        resumed = await runtime_b.resume("persistent-approve-001", approved=True)
        await runtime_b.aclose()
        return resumed

    result = asyncio.run(scenario())

    assert result.degraded is False
    assert result.pending_review is False
    assert result.response_messages


def test_async_sqlite_checkpoint_preserves_reject_path_after_restart(tmp_path):
    async def scenario():
        path = str(tmp_path / "checkpoints.db")
        runtime_a = _make_runtime("async_sqlite", path)
        user, session = _user_session("persistent-reject-001")
        await runtime_a.run(user, session, "我不想活了", "我不想活了")
        await runtime_a.aclose()

        runtime_b = _make_runtime("async_sqlite", path)
        resumed = await runtime_b.resume("persistent-reject-001", approved=False)
        await runtime_b.aclose()
        return resumed

    result = asyncio.run(scenario())

    assert result.degraded is False
    assert result.fallback_response
    assert result.response_messages == []


def test_expired_persistent_checkpoint_safely_degrades(tmp_path):
    async def scenario():
        path = str(tmp_path / "checkpoints.db")
        runtime_a = _make_runtime("async_sqlite", path)
        user, session = _user_session("persistent-expired-001")
        await runtime_a.run(user, session, "我不想活了", "我不想活了")
        await runtime_a.aclose()

        connection = sqlite3.connect(path)
        connection.execute(
            "UPDATE xling_checkpoint_activity SET updated_at = ? WHERE thread_id = ?",
            ("2000-01-01T00:00:00", "persistent-expired-001"),
        )
        connection.commit()
        connection.close()

        runtime_b = _make_runtime("async_sqlite", path)
        result = await runtime_b.resume("persistent-expired-001", approved=True)
        await runtime_b.aclose()
        return result

    result = asyncio.run(scenario())

    assert result.degraded is True
    assert result.fallback_response
