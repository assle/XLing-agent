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
from app.core.config import Settings
from app.core.enums import IntentType
from app.models.entities import ActionPlan, ChatSession, UserAccount
from app.schemas.dtos import AiMessage
from app.services.action_plan import ActionPlanService
from app.services.checkin import CheckInService
from tests.support import DatabaseHarness, FakeMemoryStore, build_runtime


def _make_runtime(backend: str, checkpoint_path: str | None = None, checkpointer=None) -> LangGraphAgentRuntimeService:
    """按后端和可选路径创建测试图执行器。

    允许注入共享保存器并重新构图，以隔离状态保存测试。
    """
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
    """创建带固定用户字段和可指定公开编号的会话。

    供状态导出、隔离和恢复测试复用。
    """
    user = UserAccount(id=1, display_name="测试学生", roles_csv="ROLE_USER")
    session = ChatSession(id=1, public_id=public_id, user_id=1)
    return user, session


def test_native_checkpoint_and_result_preserve_action_plan():
    """The support path persists primitive state while exposing typed business events."""
    harness = DatabaseHarness()
    db = harness.sessions()
    try:
        runtime = build_runtime(
            LangGraphAgentRuntimeService, db=db,
            memory=FakeMemoryStore([AiMessage(role="user", content="你好")]),
        )
        user, session = _user_session("json-safe-native")
        text = "工作加班让我担心，睡不着，还会逃避"
        result = asyncio.run(runtime.run(user, session, text))
        values = runtime.get_state(session.public_id).values
        json.dumps(values, ensure_ascii=False)
        assert "context" not in values
        assert values["user_id"] == 1
        assert values["thread_id"] == session.public_id
        assert result.action_plan_event is not None
        assert result.action_plan_event.items
        assert values["action_plan_event"]["items"][0]["content"] == result.action_plan_event.items[0].content
    finally:
        db.close()
        harness.close()


def test_support_followups_preserve_plan_edits_and_do_not_create_another_plan(monkeypatch):
    """完成追问后继续聊天复用原计划，保留替换、勾选和反馈，不重发创建事件。"""
    harness = DatabaseHarness()
    db = harness.sessions()
    try:
        user = UserAccount(username="plan-owner", display_name="测试用户", password_hash="unused")
        db.add(user)
        db.flush()
        session = ChatSession(public_id="plan-followups", title="支持计划", user_id=user.id)
        db.add(session)
        db.commit()
        runtime = build_runtime(LangGraphAgentRuntimeService, db=db, memory=FakeMemoryStore())
        generations = []
        original_generate = ActionPlanService._generate_items

        def generate_items(service, summary, user_context=""):
            generations.append((summary, user_context))
            return original_generate(service, summary, user_context)

        monkeypatch.setattr(ActionPlanService, "_generate_items", generate_items)
        text = "工作加班让我担心，睡不着，还会逃避"
        first = asyncio.run(runtime.run(user, session, text))
        db.commit()
        assert first.action_plan_event is not None
        plan_id = first.action_plan_event.plan_id
        plans = ActionPlanService(db)
        items = plans.get_plan(user.id, plan_id).items
        replacement = "十分钟整理工作安排，保留紧急通知"
        plans.replace_item(user.id, items[0].id, replacement)
        plans.mark_item_completed(user.id, items[1].id)

        correction = "我担心漏掉安排，工作通知必须保留，想调整原来的计划，不要再创建新计划"
        followup = asyncio.run(runtime.run(user, session, correction))
        db.commit()
        assert followup.cbt_event.complete is True
        assert followup.action_plan_event is None
        assert db.query(ActionPlan).count() == 1
        assert len(generations) == 1
        saved = plans.get_plan(user.id, plan_id)
        assert saved.items[0].content == replacement
        assert saved.items[1].completed is True
        instruction = "\n".join(message.content for message in followup.response_messages)
        assert replacement in instruction
        assert "本轮没有新建或改写行动项" in instruction

        CheckInService(db).submit_checkin(user.id, plan_id, "improved")
        after_feedback = asyncio.run(runtime.run(user, session, correction))
        db.commit()
        assert after_feedback.action_plan_event is None
        assert db.query(ActionPlan).count() == 1
        assert saved.status == "completed"
        assert len(generations) == 1
        completed_instruction = "\n".join(message.content for message in after_feedback.response_messages)
        assert "页面不再显示其勾选、替换或反馈入口" in completed_instruction
        assert "可建议通过计划面板的替换入口保存" not in completed_instruction

        other_session = ChatSession(public_id="plan-new-conversation", title="新支持", user_id=user.id)
        db.add(other_session)
        db.commit()
        new_support = asyncio.run(runtime.run(user, other_session, text))
        db.commit()
        assert new_support.action_plan_event is not None
        assert new_support.action_plan_event.plan_id != plan_id
        assert db.query(ActionPlan).count() == 2
    finally:
        db.close()
        harness.close()


# ---------------------------------------------------------------------------
# 验证一次运行结束后内存保存器中存在状态。
# ---------------------------------------------------------------------------

def test_state_checkpointed_after_run():
    """完成一次普通对话后读取状态快照。

    检查消息分类确实保存为日常对话。
    """
    runtime = _make_runtime("memory")
    user, session = _user_session("session-mem-001")
    asyncio.run(runtime.run(user, session, "帮我写一段 Python 代码"))
    snapshot = runtime.get_state("session-mem-001")
    assert snapshot is not None
    assert snapshot.values["intent"] == IntentType.CHAT.value


# ---------------------------------------------------------------------------
# 使用同一个保存器的新执行器实例可以读取已有状态。
# ---------------------------------------------------------------------------

def test_crash_recovery_shared_checkpointer():
    # 这里共享的是同一进程内的保存器对象，不证明真实进程重启后仍有数据。
    # 第一个实例写入，第二个实例读取同一保存器。
    """两个执行器显式共用同一个内存保存器。

    检查第二个能读取第一个的状态；这是同进程对象复用，不是重启持久性证明。
    """
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    shared_saver = MemorySaver(serde=JsonPlusSerializer())

    runtime_a = _make_runtime("memory", checkpointer=shared_saver)
    user, session = _user_session("session-crash-001")
    result_a = asyncio.run(runtime_a.run(user, session, "帮我写一段 Python 代码"))
    assert result_a.intent == IntentType.CHAT

    # 创建新的执行器实例，但继续传入同一个内存保存器。
    runtime_b = _make_runtime("memory", checkpointer=shared_saver)
    snapshot = runtime_b.get_state("session-crash-001")
    assert snapshot is not None
    assert snapshot.values["intent"] == IntentType.CHAT.value


# ---------------------------------------------------------------------------
# 不同会话编号保存各自的执行状态。
# ---------------------------------------------------------------------------

def test_thread_isolation():
    """在不同会话编号下分别运行普通与高风险消息。

    检查保存后的分类各自独立，没有相互覆盖。
    """
    runtime = _make_runtime("memory")
    user1, session1 = _user_session("session-iso-001")
    user2, session2 = _user_session("session-iso-002")
    asyncio.run(runtime.run(user1, session1, "帮我写一段 Python 代码"))
    asyncio.run(runtime.run(user2, session2, "我不想活了"))

    state1 = runtime.get_state("session-iso-001").values
    state2 = runtime.get_state("session-iso-002").values
    assert state1["intent"] == IntentType.CHAT.value
    assert state2["intent"] == IntentType.RISK.value


# ---------------------------------------------------------------------------
# 同一会话连续运行后仍能读到最新状态。
# ---------------------------------------------------------------------------

def test_same_thread_two_runs():
    """在同一会话编号下连续运行两次普通问题。

    检查第二轮结束后仍可读取有效状态。
    """
    runtime = _make_runtime("memory")
    user, session = _user_session("session-twice-001")
    asyncio.run(runtime.run(user, session, "帮我写一段 Python 代码"))
    # 第二轮在同一会话下运行，随后读取该轮保存的状态。
    asyncio.run(runtime.run(user, session, "帮我写一段 Java 代码"))
    snapshot = runtime.get_state("session-twice-001")
    assert snapshot is not None
    assert snapshot.values["intent"] == IntentType.CHAT.value


def test_async_sqlite_checkpoint_resumes_after_runtime_restart(tmp_path):
    """关闭首次执行器连接后，用新实例从同一临时文件批准恢复。

    检查没有降级且已准备回复。
    """
    async def scenario():
        """建立文件状态库并暂停会话，关闭连接后重新创建执行器。

        批准恢复并关闭新连接，返回结果给外层断言。
        """
        path = str(tmp_path / "checkpoints.db")
        runtime_a = _make_runtime("async_sqlite", path)
        user, session = _user_session("persistent-approve-001")
        interrupted = await runtime_a.run(user, session, "我不想活了")
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
    """从同一状态文件的新实例拒绝暂停会话。

    检查正常恢复拒绝路径，保留固定回复且没有模型输入。
    """
    async def scenario():
        """先保存高风险暂停状态并关闭原连接，再用新实例拒绝。

        通过实际文件读取检查状态可跨实例保留。
        """
        path = str(tmp_path / "checkpoints.db")
        runtime_a = _make_runtime("async_sqlite", path)
        user, session = _user_session("persistent-reject-001")
        await runtime_a.run(user, session, "我不想活了")
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
    """把已保存会话的活动时间设为很久以前。

    新实例恢复时应清理过期状态并安全降级。
    """
    async def scenario():
        """准备暂停状态后直接修改临时活动表时间。

        重新初始化触发过期清理，再尝试批准并返回结果。
        """
        path = str(tmp_path / "checkpoints.db")
        runtime_a = _make_runtime("async_sqlite", path)
        user, session = _user_session("persistent-expired-001")
        await runtime_a.run(user, session, "我不想活了")
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


def test_corrupt_paused_business_state_uses_safe_fallback():
    """A HIGH-risk pause cannot resume as an ordinary chat or without its assessment."""
    async def scenario(update, thread_id):
        runtime = _make_runtime("memory")
        user, session = _user_session(thread_id)
        interrupted = await runtime.run(user, session, "我不想活了")
        assert interrupted.pending_review
        config = {"configurable": {"thread_id": thread_id}}
        await runtime.graph.aupdate_state(config, update, as_node="risk_guardian")
        return await runtime.resume(thread_id, approved=True)

    updates = [
        {"intent": "CHAT"},
        {"assessment": None},
        {"assessment": {}},
        {"assessment": {
            "emotion": "ANXIETY", "emotion_score": 2.0, "risk": "LOW",
            "confidence": 0.8, "summary": "low risk", "model_version": "test",
        }},
        {"assessment": {
            "emotion": "DEPRESSED", "emotion_score": 3.0, "risk": "MEDIUM",
            "confidence": 0.8, "summary": "medium risk", "model_version": "test",
        }},
    ]
    for index, update in enumerate(updates):
        result = asyncio.run(scenario(update, f"corrupt-native-business-{index}"))
        assert result.degraded
        assert result.fallback_response
        assert result.response_messages == []
        assert not result.pending_review


def test_rising_medium_assessment_can_resume_high_risk_review():
    """A legitimate trajectory escalation remains recoverable after invariant checks."""
    from app.core.enums import EmotionLabel, RiskLevel
    from app.services.assessment import PsychologyAssessment
    from app.services.risk_trajectory import RiskTrajectoryService

    class MediumAssessment:
        async def aassess(self, text):
            return PsychologyAssessment(EmotionLabel.DEPRESSED, 3.0, RiskLevel.MEDIUM, 0.8, "持续低落")

    harness = DatabaseHarness()
    db = harness.sessions()
    try:
        trajectory = RiskTrajectoryService(db)
        trajectory.record_point(1, 1, RiskLevel.MEDIUM, 2.0)
        trajectory.record_point(1, 1, RiskLevel.MEDIUM, 2.5)
        db.commit()
        runtime = build_runtime(
            LangGraphAgentRuntimeService, db=db, assessment=MediumAssessment(),
            memory=FakeMemoryStore([AiMessage(role="user", content="你好")]),
        )
        user, session = _user_session("native-rising-medium-review")

        async def scenario():
            paused = await runtime.run(user, session, "最近压力很大，睡不着")
            assert paused.pending_review
            assert paused.assessment.risk == RiskLevel.MEDIUM
            assert paused.risk_level == RiskLevel.HIGH
            assert paused.trajectory_rising
            return await runtime.resume(session.public_id, approved=True)

        result = asyncio.run(scenario())
        assert not result.degraded
        assert result.response_messages
        assert result.risk_level == RiskLevel.HIGH
    finally:
        db.close()
        harness.close()
