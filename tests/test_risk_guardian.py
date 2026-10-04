"""Tests for independent message type and safety risk assessment.

Issue 01: Assessment 三层风险评估单测基线 (runtime-layer override).

An explicit high-risk signal still produces HIGH, while the RISK message type
does not overwrite an independent assessment result.

Run: python -m pytest tests/test_risk_guardian.py
"""
from __future__ import annotations

import asyncio

from app.agents.langgraph_runtime import LangGraphAgentRuntimeService
from app.core.enums import IntentType, RiskLevel
from app.models.entities import ChatSession, UserAccount
from app.schemas.dtos import AiMessage
from app.services.assessment import PsychologicalAssessmentService
from tests.support import FakeMemoryStore, build_runtime


class FakeAiClient:
    """Returns a canned response for both sync complete and async acomplete."""

    def __init__(self, response: str):
        """保存用于风险守护步骤测试的结构化回复。

        后续同步和异步方法都使用同一文本。
        """
        self._response = response

    def complete(self, messages):  # noqa: ANN001
        """返回预设完整回复，不执行网络请求。

        让测试只关注风险规则和状态更新。
        """
        return self._response

    async def acomplete(self, messages):  # noqa: ANN001
        """异步返回相同预设回复。

        兼容执行器的异步调用接口。
        """
        return self._response

    def classify(self, text: str) -> str:
        """从预设回复的 emotion 字段转换为中文分类标签。

        解析失败或未知情绪按正常返回，这是测试替身的简化规则。
        """
        import json
        try:
            data = json.loads(self._response)
            emotion = data.get("emotion", "NORMAL")
        except Exception:
            emotion = "NORMAL"
        return {"NORMAL": "正常", "ANXIETY": "焦虑", "DEPRESSED": "低落", "HIGH_RISK": "高风险"}.get(emotion, "正常")

    async def aclassify(self, text: str) -> str:
        """异步转发分类替身结果。

        保留与同步方法相同的标签映射。
        """
        return self.classify(text)


def _make_runtime(llm_response: str) -> LangGraphAgentRuntimeService:
    return build_runtime(
        LangGraphAgentRuntimeService,
        memory=FakeMemoryStore([AiMessage(role="user", content="你好")]),
        assessment=PsychologicalAssessmentService(FakeAiClient(llm_response)),
    )


def _run(runtime, intent: IntentType, text: str = "有点担心", *, thread_id="risk-independent"):
    class RoutingAi:
        async def acomplete(self, messages):
            return intent.value

        def complete(self, messages):
            return "{}"

    runtime.ai = RoutingAi()
    user = UserAccount(id=1, display_name="测试用户", roles_csv="ROLE_USER")
    session = ChatSession(id=1, public_id=thread_id, user_id=1)
    return asyncio.run(runtime.run(user, session, text))


def test_risk_intent_preserves_low_assessment():
    runtime = _make_runtime('{"emotion":"NORMAL","emotionScore":0.0,"risk":"LOW","confidence":0.5,"summary":"ok"}')
    result = _run(runtime, IntentType.RISK, thread_id="risk-low")
    assert result.intent == IntentType.RISK
    assert result.assessment.risk == RiskLevel.LOW
    assert result.assessment.emotion_score == 0.0
    assert result.risk_level == RiskLevel.LOW
    assert not result.pending_review


def test_risk_intent_keeps_high_when_already_high():
    runtime = _make_runtime('{"emotion":"HIGH_RISK","emotionScore":4.0,"risk":"HIGH","confidence":0.9,"summary":"danger"}')
    result = _run(runtime, IntentType.RISK, "我很危险", thread_id="risk-high")
    assert result.assessment.risk == RiskLevel.HIGH
    assert result.pending_review


def test_risk_intent_preserves_medium_assessment():
    runtime = _make_runtime('{"emotion":"DEPRESSED","emotionScore":3.0,"risk":"MEDIUM","confidence":0.7,"summary":"low"}')
    result = _run(runtime, IntentType.RISK, "心情不好", thread_id="risk-medium")
    assert result.assessment.risk == RiskLevel.MEDIUM
    assert not result.pending_review


def test_consult_intent_not_overridden():
    runtime = _make_runtime('{"emotion":"ANXIETY","emotionScore":2.0,"risk":"LOW","confidence":0.7,"summary":"stress"}')
    result = _run(runtime, IntentType.CONSULT, "有点焦虑", thread_id="support-low")
    assert result.assessment.risk == RiskLevel.LOW


def test_chat_intent_skips_risk_guardian():
    runtime = _make_runtime('{"emotion":"NORMAL","emotionScore":0.0,"risk":"LOW","confidence":0.5,"summary":"x"}')
    result = _run(runtime, IntentType.CHAT, "今天天气不错", thread_id="chat-no-assessment")
    assert result.assessment is None
    assert not result.pending_review


def test_resume_does_not_repeat_risk_assessment():
    runtime = _make_runtime('{"emotion":"HIGH_RISK","emotionScore":4.0,"risk":"HIGH","confidence":0.9,"summary":"danger"}')
    original = runtime.assessment.aassess
    calls = []

    async def counted(text):
        calls.append(text)
        return await original(text)

    runtime.assessment.aassess = counted
    interrupted = _run(runtime, IntentType.RISK, "我不想活了", thread_id="risk-once-on-resume")
    assert interrupted.pending_review
    resumed = asyncio.run(runtime.resume("risk-once-on-resume", approved=True))
    assert resumed.response_messages
    assert calls == ["我不想活了"]


def test_trajectory_failure_rolls_back_before_safe_fallback():
    class BrokenDb:
        rollback_called = False

        def add(self, item):
            raise RuntimeError("trajectory write failed")

        def rollback(self):
            self.rollback_called = True

    runtime = _make_runtime('{"emotion":"ANXIETY","emotionScore":2.0,"risk":"LOW","confidence":0.7,"summary":"stress"}')
    runtime.db = BrokenDb()
    result = _run(runtime, IntentType.CONSULT, thread_id="trajectory-safe-fallback")
    assert runtime.db.rollback_called
    assert result.risk_level == RiskLevel.LOW
    assert result.response_messages
