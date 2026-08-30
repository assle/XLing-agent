"""End-to-end tracer bullet test: complete closed-loop (issue 12).

Proves the full user journey:
  进入 -> 风险分流 -> CBT 追问 -> 行动计划 -> 次日 check-in -> 升级或完成

Two scenarios:
  1. Normal completion: 4 CBT dimensions -> action plan -> check-in improved -> completed
  2. Escalation: 4 CBT dimensions -> action plan -> check-in worsened -> SUSTAINED_NO_IMPROVEMENT

Uses mock AI + stateful fake memory (persists CBT state) + real SQLite DB.
No external services (Redis, MySQL, Ollama, OpenAI) required.

Run: python -m pytest tests/test_tracer_bullet.py
"""
from __future__ import annotations

import asyncio

from app.agents.runtime import AgentRuntimeService
from app.core.config import Settings
from app.core.security import hash_password
from app.models.entities import ActionPlan, ChatSession, UserAccount
from app.schemas.dtos import AiMessage
from tests.support import DatabaseHarness, build_runtime

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class StatefulFakeMemory:
    """Fake memory store that persists CBT state across messages."""

    def __init__(self):
        self._cbt_states: dict[str, dict] = {}

    def load_recent(self, session_public_id: str) -> list[AiMessage]:
        return []

    def messages_from_rows(self, rows):
        return []

    def replace(self, session_public_id: str, messages: list[AiMessage]) -> None:
        pass

    def save_cbt_state(self, session_public_id: str, state: dict) -> None:
        self._cbt_states[session_public_id] = state

    def load_cbt_state(self, session_public_id: str) -> dict:
        return self._cbt_states.get(session_public_id, {})


class FakeKnowledge:
    def retrieve(self, query: str, top_k: int | None = None):
        return []


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

_TestSession = DatabaseHarness().sessions

_settings = Settings(ai_provider="mock", langgraph_checkpoint_backend="memory", knowledge_vector_enabled=False)


def _seed():
    db = _TestSession()
    try:
        user = UserAccount(username="student", display_name="测试学生", password_hash=hash_password("s"))
        user.roles = {"ROLE_USER"}
        session = ChatSession(public_id="tracer-session", title="tracer bullet", user_id=1)
        db.add_all([user, session])
        db.commit()
        # Add user profile with exam stage
        from app.services.user_profile import UserProfileService
        UserProfileService(db).update_profile(1, exam_stage="冲刺", target_exam="考研")
    finally:
        db.close()


_seed()


def _make_runtime(db):
    """Create a custom runtime with fake deps but real DB for CBT/action plan."""
    return build_runtime(
        AgentRuntimeService,
        db=db,
        settings=_settings,
        memory=StatefulFakeMemory(),
        knowledge=FakeKnowledge(),
    )


async def _run_message(runtime, message: str):
    """Run a single message through the runtime."""
    db = _TestSession()
    try:
        runtime.db = db
        user = db.get(UserAccount, 1)
        session = db.query(ChatSession).filter(ChatSession.public_id == "tracer-session").first()
        result = await runtime.run(user, session, message, message)
        return result
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Scenario 1: Normal completion
# ---------------------------------------------------------------------------

def test_tracer_bullet_normal_completion():
    """Full closed-loop: CBT 4 dimensions -> action plan -> check-in improved -> completed."""
    runtime = _make_runtime(None)

    # Message 1: trigger event (考研, 复习)
    result1 = asyncio.run(_run_message(runtime, "最近考研复习压力很大"))
    assert result1.intent.value == "CONSULT"
    assert result1.risk_level.value in ("LOW", "MEDIUM")
    # CBT should be active, asking about trigger event or next dimension
    assert len(result1.response_messages) > 0

    # Message 2: thoughts (觉得, 担心)
    result2 = asyncio.run(_run_message(runtime, "我觉得自己肯定考不上，很焦虑"))
    assert len(result2.response_messages) > 0

    # Message 3: body reactions (失眠, 心跳)
    result3 = asyncio.run(_run_message(runtime, "最近总是失眠，心跳很快"))
    assert len(result3.response_messages) > 0

    # Message 4: behavior (逃避, 拖延)
    result4 = asyncio.run(_run_message(runtime, "我开始逃避复习，压力很大一直拖延"))
    assert len(result4.response_messages) > 0

    # Typed loop events ride on the run result -- no string parsing downstream
    assert result1.cbt_event is not None and result1.cbt_event.active is True
    assert result4.cbt_event is not None and result4.cbt_event.complete is True
    assert result4.cbt_event.completed_count == 4
    assert result4.action_plan_event is not None
    assert len(result4.action_plan_event.items) >= 1

    # Verify action plan was created
    db = _TestSession()
    try:
        plans = db.query(ActionPlan).filter(ActionPlan.user_id == 1).all()
        assert len(plans) >= 1, "Action plan should be created after CBT completion"
        plan = plans[-1]
        assert plan.status == "active"
        assert len(plan.items) >= 1
        assert plan.target_window_hours == 24
        assert result4.action_plan_event.plan_id == plan.id
    finally:
        db.close()

    # Simulate next-day check-in: improved
    from app.services.checkin import CheckInService
    db = _TestSession()
    try:
        plan_id = db.query(ActionPlan).filter(ActionPlan.user_id == 1).first().id
        checkin = CheckInService(db).submit_checkin(1, plan_id, "improved", "feeling better")
        assert checkin.improvement_status == "improved"
        plan = db.get(ActionPlan, plan_id)
        assert plan.status == "completed"
    finally:
        db.close()

    print("  Normal completion: CBT -> action plan -> check-in improved -> completed")


# ---------------------------------------------------------------------------
# Scenario 2: Escalation via worsened check-in
# ---------------------------------------------------------------------------

def test_tracer_bullet_escalation_worsened():
    """Full closed-loop with escalation: CBT -> action plan -> check-in worsened -> escalation."""
    # Clean up from previous test
    db = _TestSession()
    try:
        from app.models.entities import ActionPlanItem, CheckIn, RiskTrajectoryPoint
        for model in [CheckIn, ActionPlanItem, ActionPlan, RiskTrajectoryPoint]:
            db.query(model).delete()
        db.commit()
    finally:
        db.close()

    # Reset CBT state
    runtime = _make_runtime(None)
    runtime.memory = StatefulFakeMemory()  # fresh CBT state

    # Run 4 CBT messages
    for msg in [
        "最近考研复习压力很大",
        "我觉得自己肯定考不上，很焦虑",
        "最近总是失眠，心跳很快",
        "我开始逃避复习，压力很大一直拖延",
    ]:
        asyncio.run(_run_message(runtime, msg))

    # Verify action plan created
    db = _TestSession()
    try:
        plans = db.query(ActionPlan).filter(ActionPlan.user_id == 1).all()
        assert len(plans) >= 1, "Action plan should be created"
        plan_id = plans[-1].id
    finally:
        db.close()

    # Simulate check-in: worsened -> should trigger escalation
    from app.services.checkin import CheckInService
    from app.services.escalation import EscalationService
    from app.services.review import ReviewService
    db = _TestSession()
    try:
        checkin = CheckInService(db).submit_checkin(1, plan_id, "worsened", "feeling worse")
        assert checkin.improvement_status == "worsened"

        # Check escalation
        esc_svc = EscalationService(db, ReviewService(db, _settings))
        result = esc_svc.check_checkin_escalation(
            user_id=1, session_id=1, report_id=1, thread_id="tracer-session",
            improvement_status="worsened",
            current_difficulty="考研焦虑恶化",
        )
        assert result.should_escalate is True
        assert result.handoff_reason == "SUSTAINED_NO_IMPROVEMENT"
        assert result.review_id is not None
        assert "400-161-9995" in result.user_message  # safety message with hotline
    finally:
        db.close()

    print("  Escalation: CBT -> action plan -> check-in worsened -> SUSTAINED_NO_IMPROVEMENT review")


# ---------------------------------------------------------------------------
# Scenario 3: CHAT path doesn't enter CBT
# ---------------------------------------------------------------------------

def test_chat_path_skips_cbt():
    """CHAT intent should not enter CBT flow."""
    runtime = _make_runtime(None)
    runtime.memory = StatefulFakeMemory()
    result = asyncio.run(_run_message(runtime, "Python 怎么读取 JSON 文件？"))
    assert result.intent.value == "CHAT"
    assert len(result.response_messages) > 0
    # CBT state should be empty (never entered)
    assert runtime.memory.load_cbt_state("tracer-session") == {}
