"""Tests for RiskGuardianAgent intent==RISK override logic.

Issue 01: Assessment 三层风险评估单测基线 (runtime-layer override).

RiskGuardianAgent forces risk=HIGH when intent==RISK and the assessment
did not already return HIGH. This is the trigger precondition for the
human-review interrupt (issue 04).

Run:  python tests/test_risk_guardian.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.enums import IntentType, RiskLevel
from app.models.entities import ChatSession, UserAccount
from app.agents.runtime import AgentContext, AgentRuntimeService
from app.services.assessment import PsychologicalAssessmentService


class FakeAiClient:
    """Returns a canned response for both sync complete and async acomplete."""

    def __init__(self, response: str):
        self._response = response

    def complete(self, messages):  # noqa: ANN001
        return self._response

    async def acomplete(self, messages):  # noqa: ANN001
        return self._response

    def classify(self, text: str) -> str:
        """Derive a Chinese label from the canned JSON response."""
        import json
        try:
            data = json.loads(self._response)
            emotion = data.get("emotion", "NORMAL")
        except Exception:
            emotion = "NORMAL"
        return {"NORMAL": "正常", "ANXIETY": "焦虑", "DEPRESSED": "低落", "HIGH_RISK": "高风险"}.get(emotion, "正常")

    async def aclassify(self, text: str) -> str:
        return self.classify(text)


def _make_runtime(llm_response: str) -> AgentRuntimeService:
    """Bypass __init__ to avoid db/redis/chromadb -- only self.assessment is needed."""
    runtime = AgentRuntimeService.__new__(AgentRuntimeService)
    runtime.assessment = PsychologicalAssessmentService(FakeAiClient(llm_response))
    return runtime


def _make_context(intent: IntentType, text: str = "有点担心") -> AgentContext:
    return AgentContext(
        user=UserAccount(),
        session=ChatSession(),
        original_input=text,
        model_input=text,
        memory_loaded=True,
        intent_routed=True,
        knowledge_handled=True,
        risk_assessed=False,
        intent=intent,
    )


# ---------------------------------------------------------------------------
# intent == RISK forces HIGH even when LLM says LOW
# ---------------------------------------------------------------------------

def test_risk_intent_overrides_low_to_high():
    runtime = _make_runtime('{"emotion":"NORMAL","emotionScore":0.0,"risk":"LOW","confidence":0.5,"summary":"ok"}')
    context = _make_context(IntentType.RISK)
    proceeded = asyncio.run(runtime.risk_guardian_agent(4, context))
    assert proceeded is True
    assert context.assessment.risk == RiskLevel.HIGH
    assert context.assessment.emotion_score >= 4.0
    assert context.risk_level == RiskLevel.HIGH
    assert context.risk_assessed is True


def test_risk_intent_keeps_high_when_already_high():
    runtime = _make_runtime('{"emotion":"HIGH_RISK","emotionScore":4.0,"risk":"HIGH","confidence":0.9,"summary":"danger"}')
    context = _make_context(IntentType.RISK, "我很危险")
    asyncio.run(runtime.risk_guardian_agent(4, context))
    assert context.assessment.risk == RiskLevel.HIGH


def test_risk_intent_overrides_medium_to_high():
    runtime = _make_runtime('{"emotion":"DEPRESSED","emotionScore":3.0,"risk":"MEDIUM","confidence":0.7,"summary":"low"}')
    context = _make_context(IntentType.RISK, "心情不好")
    asyncio.run(runtime.risk_guardian_agent(4, context))
    assert context.assessment.risk == RiskLevel.HIGH


# ---------------------------------------------------------------------------
# intent != RISK -> no override
# ---------------------------------------------------------------------------

def test_consult_intent_not_overridden():
    runtime = _make_runtime('{"emotion":"ANXIETY","emotionScore":2.0,"risk":"LOW","confidence":0.7,"summary":"stress"}')
    context = _make_context(IntentType.CONSULT, "有点焦虑")
    asyncio.run(runtime.risk_guardian_agent(4, context))
    assert context.assessment.risk == RiskLevel.LOW


def test_chat_intent_skips_risk_guardian():
    runtime = _make_runtime('{"emotion":"NORMAL","emotionScore":0.0,"risk":"LOW","confidence":0.5,"summary":"x"}')
    context = _make_context(IntentType.CHAT, "今天天气不错")
    proceeded = asyncio.run(runtime.risk_guardian_agent(4, context))
    assert proceeded is False
    assert context.assessment is None  # never assessed


# ---------------------------------------------------------------------------
# Already assessed -> skip
# ---------------------------------------------------------------------------

def test_already_assessed_skips():
    runtime = _make_runtime('{"emotion":"NORMAL","emotionScore":0.0,"risk":"LOW","confidence":0.5,"summary":"x"}')
    context = _make_context(IntentType.RISK)
    context.risk_assessed = True
    proceeded = asyncio.run(runtime.risk_guardian_agent(4, context))
    assert proceeded is False


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
