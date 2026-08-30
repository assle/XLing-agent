"""Tests for SSE event extension (frontend-adaptation spec).

Verifies the external behaviour of ChatService.stream_chat:
  - `cbt` event (active / completedCount / nextDimension / complete) is emitted
    after `meta` and before `token` when the agent run contains CBT steps.
  - `action_plan` event (planId / items with id+content+order) is emitted when
    CBT completes and a 24h action plan is generated.
  - No `cbt` / `action_plan` events on a plain CHAT run.
  - `noMemory` on ChatRequest flags the created / fetched ChatSession.

Uses a stub agent runtime + fake memory + real SQLite DB.
No external services (Redis, MySQL, Ollama, OpenAI) required.

Run: python -m pytest tests/test_sse_events.py
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import app.services.chat as chat_module
from app.agents.runtime import ActionPlanEvent, ActionPlanItemEvent, AgentRunResult, AgentStep, CbtEvent
from app.core.config import Settings
from app.core.enums import EmotionLabel, IntentType, RiskLevel
from app.core.security import hash_password
from app.models.entities import ChatSession, ReviewRequest, UserAccount
from app.schemas.dtos import AiMessage, ChatRequest
from app.services.assessment import PsychologyAssessment
from tests.support import DatabaseHarness

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

_TestSession = DatabaseHarness().sessions

_settings = Settings(ai_provider="mock", langgraph_checkpoint_backend="memory", knowledge_vector_enabled=False)

db = _TestSession()
user = UserAccount(username="sse-student", display_name="SSE 学生", password_hash=hash_password("s"))
user.roles = {"ROLE_USER"}
db.add(user)
db.commit()
USER_ID = user.id
db.close()


class _FakeMemory:
    def append(self, session_public_id: str, role: str, content: str) -> None:
        pass

    def load_recent(self, session_public_id: str):
        return []

    def save_cbt_state(self, session_public_id: str, state: dict) -> None:
        pass

    def load_cbt_state(self, session_public_id: str) -> dict:
        return {}


def _assessment() -> PsychologyAssessment:
    return PsychologyAssessment(
        emotion=EmotionLabel.ANXIETY,
        emotion_score=0.6,
        risk=RiskLevel.LOW,
        confidence=0.9,
        summary="考研焦虑",
    )


class _StubRuntime:
    """Returns a canned AgentRunResult with given steps."""

    def __init__(self, steps, intent=IntentType.CONSULT, pending_review=False,
                 trajectory_rising=False, trajectory_trend="",
                 cbt_event=None, action_plan_event=None):
        self._steps = steps
        self._intent = intent
        self._pending_review = pending_review
        self._trajectory_rising = trajectory_rising
        self._trajectory_trend = trajectory_trend
        self._cbt_event = cbt_event
        self._action_plan_event = action_plan_event

    async def run(self, user, session, text, model_input) -> AgentRunResult:
        assessment = _assessment()
        if self._pending_review:
            assessment.risk = RiskLevel.HIGH
        return AgentRunResult(
            intent=self._intent,
            risk_level=assessment.risk,
            assessment=assessment,
            retrieved_knowledge=[],
            response_messages=[AiMessage(role="user", content=model_input)],
            steps=self._steps,
            pending_review=self._pending_review,
            trajectory_rising=self._trajectory_rising,
            trajectory_trend=self._trajectory_trend,
            cbt_event=self._cbt_event,
            action_plan_event=self._action_plan_event,
        )


class _NoopReportDispatcher:
    async def dispatch(self, report_id: int, risk_level: str | None) -> None:
        pass


def _run_stream(
    steps,
    message="我最近压力很大",
    intent=IntentType.CONSULT,
    no_memory=None,
    pending_review=False,
    trajectory_rising=False,
    trajectory_trend="",
    cbt_event=None,
    action_plan_event=None,
):
    """Drive ChatService.stream_chat with a stub runtime, return parsed SSE events."""
    runtime = _StubRuntime(
        steps, intent, pending_review, trajectory_rising, trajectory_trend, cbt_event, action_plan_event
    )
    db = _TestSession()
    try:
        dependencies = replace(
            chat_module.ChatDependencies.create(db, _settings),
            memory=_FakeMemory(),
            runtime_factory=lambda db, settings: runtime,
            report_dispatcher=_NoopReportDispatcher(),
        )
        service = chat_module.ChatService(db, _settings, dependencies)
        request = ChatRequest(message=message, noMemory=no_memory)
        raw_events = []

        async def collect():
            async for chunk in service.stream_chat(db.get(UserAccount, USER_ID), request):
                raw_events.append(chunk)

        asyncio.run(collect())
    finally:
        db.close()

    events = []
    for chunk in raw_events:
        lines = [line for line in chunk.strip().split("\n") if line]
        event_name = next((line[7:] for line in lines if line.startswith("event: ")), None)
        data_line = next((line for line in lines if line.startswith("data: ")), None)
        events.append((event_name, json.loads(data_line[6:]) if data_line else {}))
    return events


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_cbt_event_emitted_between_meta_and_token():
    """CBT ask event -> SSE `cbt` event with active=true, count and next dimension."""
    events = _run_stream(
        [], cbt_event=CbtEvent(active=True, completed_count=2, next_dimension="body_reactions", complete=False)
    )
    names = [name for name, _ in events]
    assert "cbt" in names, f"cbt event missing, got {names}"
    assert names.index("meta") < names.index("cbt") < names.index("token"), f"bad order: {names}"
    cbt = next(data for name, data in events if name == "cbt")
    assert cbt["active"] is True
    assert cbt["completedCount"] == 2
    assert cbt["nextDimension"] == "body_reactions"
    assert cbt["complete"] is False
    print("  cbt event emitted with progress fields")


def test_action_plan_event_on_cbt_completion():
    """CBT complete + plan events -> cbt complete SSE + action_plan SSE with items."""
    plan_event = ActionPlanEvent(
        plan_id=42,
        items=[
            ActionPlanItemEvent(id=1, content="出门走 10 分钟", order=1, completed=False),
            ActionPlanItemEvent(id=2, content="睡前不看手机", order=2, completed=False),
        ],
    )
    events = _run_stream(
        [],
        cbt_event=CbtEvent(active=False, completed_count=4, next_dimension=None, complete=True),
        action_plan_event=plan_event,
    )
    names = [name for name, _ in events]
    assert "cbt" in names and "action_plan" in names, f"missing events: {names}"
    assert names.index("meta") < names.index("cbt") < names.index("token")
    assert names.index("meta") < names.index("action_plan") < names.index("token")
    cbt = next(data for name, data in events if name == "cbt")
    assert cbt["complete"] is True and cbt["active"] is False
    plan = next(data for name, data in events if name == "action_plan")
    assert plan["planId"] == 42
    assert plan["feedbackAvailable"] is True
    assert [item["content"] for item in plan["items"]] == ["出门走 10 分钟", "睡前不看手机"]
    assert all("id" in item and "order" in item for item in plan["items"])
    print("  action_plan event emitted with plan items")


def test_chat_intent_emits_no_loop_events():
    """Plain CHAT run -> no cbt / action_plan events."""
    steps = [AgentStep(1, "SupervisorAgent", "ROUTE_INTENT", "intent=CHAT")]
    events = _run_stream(steps, message="Python 怎么读取 JSON？", intent=IntentType.CHAT)
    names = [name for name, _ in events]
    assert "cbt" not in names and "action_plan" not in names
    print("  no loop events on plain chat")


def test_no_memory_flag_on_new_session():
    """noMemory=true on request -> created session flagged no_memory."""
    events = _run_stream([], message="不想被记住的话", no_memory=True)
    meta = next(data for name, data in events if name == "meta")
    db = _TestSession()
    try:
        session = db.query(ChatSession).filter(ChatSession.public_id == meta["sessionId"]).first()
        assert session is not None
        assert session.no_memory is True, "session should be flagged no_memory"
    finally:
        db.close()
    print("  noMemory=true flags new session")


def test_default_session_not_no_memory():
    """Omitting noMemory -> session.no_memory stays False."""
    events = _run_stream([], message="普通一句话")
    meta = next(data for name, data in events if name == "meta")
    db = _TestSession()
    try:
        session = db.query(ChatSession).filter(ChatSession.public_id == meta["sessionId"]).first()
        assert session.no_memory is False
    finally:
        db.close()
    print("  default session is not no-memory")


def test_existing_no_memory_session_cannot_be_reenabled():
    from app.services.chat import ChatService

    db = _TestSession()
    try:
        session = ChatSession(public_id="immutable-no-memory", title="private", user_id=USER_ID, no_memory=True)
        db.add(session)
        db.commit()
        service = ChatService(db, _settings)
        resolved = service.resolve_session(
            db.get(UserAccount, USER_ID), session.public_id, "继续说", no_memory=False
        )
        assert resolved.no_memory is True
    finally:
        db.close()


def test_pending_review_has_reason_and_desensitized_summary():
    """High-risk handoff -> admin-facing review has a reason and sanitized context."""
    events = _run_stream(
        [],
        message="我叫小明，手机号 13812345678，最近有伤害自己的念头",
        intent=IntentType.RISK,
        pending_review=True,
    )
    meta = next(data for name, data in events if name == "meta")
    db = _TestSession()
    try:
        session = db.query(ChatSession).filter(ChatSession.public_id == meta["sessionId"]).one()
        review = db.query(ReviewRequest).filter(ReviewRequest.session_id == session.id).one()
        assert review.handoff_reason == "HIGH_RISK_KEYWORD"
        assert "当前困境" in review.desensitized_summary
        assert "13812345678" not in review.desensitized_summary
        assert "[手机号已隐藏]" in review.desensitized_summary
    finally:
        db.close()
    print("  pending review includes sanitized handoff context")


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Review creation: one entry (ReviewService), correct handoff reason
# ---------------------------------------------------------------------------

def _latest_review():
    db = _TestSession()
    try:
        return (
            db.query(ReviewRequest)
            .order_by(ReviewRequest.id.desc())
            .first()
        )
    finally:
        db.close()


def test_pending_review_keyword_reason_and_chinese_trend():
    """Explicit HIGH message -> HIGH_RISK_KEYWORD review with a Chinese,
    honest trend label (no more English placeholder)."""
    _run_stream([], pending_review=True)
    review = _latest_review()
    assert review is not None
    assert review.handoff_reason == "HIGH_RISK_KEYWORD"
    assert review.status == "pending"
    assert "单条消息达到高风险" in review.desensitized_summary
    assert "single-message" not in review.desensitized_summary
    print("  keyword review labeled correctly")


def test_pending_review_trajectory_reason_when_rising():
    """Trajectory-raised HIGH -> RISK_TRAJECTORY_RISING review carrying the
    real trend text from the risk guardian."""
    _run_stream(
        [],
        pending_review=True,
        trajectory_rising=True,
        trajectory_trend="连续上升（近 7 天 5 个记录点，最新风险 HIGH）",
    )
    review = _latest_review()
    assert review is not None
    assert review.handoff_reason == "RISK_TRAJECTORY_RISING"
    assert "连续上升" in review.desensitized_summary
    print("  trajectory review labeled correctly")


# Runner
# ---------------------------------------------------------------------------
