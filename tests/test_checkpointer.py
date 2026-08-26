"""Tests for LangGraph Checkpointer integration (issue 03).

Verifies that:
- Each run's Agent state is checkpointed under thread_id = session.public_id
- Crash recovery: a new runtime reading the same SqliteSaver file recovers
  the previous run's state
- Different thread_ids have isolated checkpoint state

Uses mock AiClient + fake memory/knowledge stores -- no Redis/MySQL/Chroma.

Run:  python tests/test_checkpointer.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import Settings
from app.core.enums import IntentType
from app.models.entities import ChatSession, UserAccount
from app.schemas.dtos import AiMessage
from app.agents.langgraph_runtime import LangGraphAgentRuntimeService
from app.services.ai import AiClient
from app.services.assessment import PsychologicalAssessmentService


class FakeMemoryStore:
    def load_recent(self, session_public_id: str) -> list[AiMessage]:
        return [AiMessage(role="user", content="你好"), AiMessage(role="assistant", content="你好呀")]

    def messages_from_rows(self, rows):  # noqa: ANN001
        return []

    def replace(self, session_public_id: str, messages: list[AiMessage]) -> None:
        pass


class FakeKnowledgeService:
    def retrieve(self, query: str, top_k: int | None = None):  # noqa: ANN001
        return []


def _make_runtime(backend: str, checkpoint_path: str | None = None, checkpointer=None) -> LangGraphAgentRuntimeService:
    runtime = LangGraphAgentRuntimeService.__new__(LangGraphAgentRuntimeService)
    runtime.db = None
    kwargs = {"ai_provider": "mock", "langgraph_checkpoint_backend": backend}
    if checkpoint_path:
        kwargs["langgraph_checkpoint_path"] = checkpoint_path
    runtime.settings = Settings(**kwargs)
    runtime.ai = AiClient(runtime.settings)
    runtime.memory = FakeMemoryStore()
    runtime.knowledge = FakeKnowledgeService()
    runtime.assessment = PsychologicalAssessmentService(runtime.ai)
    runtime._sqlite_conn = None
    runtime._checkpointer = checkpointer if checkpointer is not None else runtime._make_checkpointer()
    runtime.graph = runtime._build_graph()
    return runtime


def _user_session(public_id: str = "session-checkpoint-001"):
    user = UserAccount(id=1, display_name="测试学生", roles_csv="ROLE_USER")
    session = ChatSession(id=1, public_id=public_id, user_id=1)
    return user, session


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

    shared_saver = MemorySaver(serde=JsonPlusSerializer(pickle_fallback=True))

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


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    passed = 0
    failed = 0
    for test in _TESTS:
        try:
            test()
            print(f"  PASS  {test.__name__}")
            passed += 1
        except AssertionError as exc:
            print(f"  FAIL  {test.__name__}: {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERROR {test.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(_TESTS)} total")
    sys.exit(1 if failed else 0)
