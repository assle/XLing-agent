"""Tests for PsychologicalAssessmentService structured output + retry.

Issue 02: Structured risk output + tenacity bounded retry.

Covers:
  1. Keyword direct judgment (has_high_risk_signal -> HIGH, no LLM)
  2. LLM schema-validated assessment (psychology_prompt -> RiskAssessmentSchema)
  3. risk_from_score cross-check (take highest) + HIGH_RISK emotion force HIGH
  4. Schema validation failure -> retry -> safe fallback
  5. Transient error (timeout) -> retry -> safe fallback
  6. Permanent error (4xx) -> no retry -> safe fallback
  7. JSON parse error -> retry -> safe fallback
  8. Safe fallback is conservative (MEDIUM risk)

Run:  python tests/test_assessment.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from app.core.enums import EmotionLabel, RiskLevel
from app.services.assessment import (
    PsychologyAssessment,
    PsychologicalAssessmentService,
    RiskAssessmentSchema,
    safe_fallback_assessment,
    heuristic,
    risk_from_score,
    risk_order,
    score_for_emotion,
    TransientAssessmentError,
    PermanentAssessmentError,
)


class FakeAiClient:
    """Test double for AiClient with configurable error behavior."""

    def __init__(self, response: str = "", error_type: str | None = None):
        self._response = response
        self._error_type = error_type
        self.call_count = 0

    def complete(self, messages):  # noqa: ANN001
        self.call_count += 1
        if self._error_type == "transient":
            raise httpx.TimeoutException("simulated timeout")
        if self._error_type == "permanent":
            raise httpx.HTTPStatusError(
                "simulated 401", request=httpx.Request("POST", "http://x"), response=httpx.Response(401)
            )
        if self._error_type == "runtime":
            raise RuntimeError("simulated unexpected error")
        return self._response

    async def acomplete(self, messages):  # noqa: ANN001
        return self.complete(messages)


def _service(response: str = "", error_type: str | None = None, max_retries: int = 2) -> PsychologicalAssessmentService:
    return PsychologicalAssessmentService(FakeAiClient(response, error_type), max_retries=max_retries)


# ---------------------------------------------------------------------------
# Layer 1: high-risk keyword -> direct HIGH, no LLM call
# ---------------------------------------------------------------------------

def test_high_risk_keyword_skips_llm_suicide():
    svc = _service('{"emotion":"NORMAL","emotionScore":0.0,"risk":"LOW","confidence":0.5,"summary":"x"}')
    result = svc.assess("我不想活了，想自杀")
    assert result.emotion == EmotionLabel.HIGH_RISK
    assert result.risk == RiskLevel.HIGH
    assert svc.ai.call_count == 0


def test_high_risk_keyword_skips_llm_selfharm():
    svc = _service('{"emotion":"NORMAL","emotionScore":0.0,"risk":"LOW","confidence":0.5,"summary":"x"}')
    result = svc.assess("我想伤害自己")
    assert result.risk == RiskLevel.HIGH
    assert svc.ai.call_count == 0


# ---------------------------------------------------------------------------
# Layer 2: LLM schema-validated assessment (legacy general-LLM path, retained)
# These tests exercise the retained _assess_with_retry path directly.
# ---------------------------------------------------------------------------

def test_llm_anxiety_low():
    svc = _service('{"emotion":"ANXIETY","emotionScore":2.5,"risk":"LOW","confidence":0.72,"summary":"stress"}')
    result = svc._assess_with_retry("最近考试压力很大", [])
    assert result.emotion == EmotionLabel.ANXIETY
    assert result.emotion_score == 2.5
    assert result.risk == RiskLevel.LOW


def test_llm_depressed_medium():
    svc = _service('{"emotion":"DEPRESSED","emotionScore":3.1,"risk":"MEDIUM","confidence":0.8,"summary":"low mood"}')
    result = svc._assess_with_retry("最近一直很低落", [])
    assert result.emotion == EmotionLabel.DEPRESSED
    assert result.risk == RiskLevel.MEDIUM


# ---------------------------------------------------------------------------
# Layer 3: risk_from_score cross-check -- take highest
# ---------------------------------------------------------------------------

def test_score_overrides_llm_risk_low_to_high():
    svc = _service('{"emotion":"NORMAL","emotionScore":4.0,"risk":"LOW","confidence":0.5,"summary":"x"}')
    result = svc._assess_with_retry("有点担心", [])
    assert result.risk == RiskLevel.HIGH


def test_score_does_not_downgrade_llm_risk():
    svc = _service('{"emotion":"ANXIETY","emotionScore":2.0,"risk":"MEDIUM","confidence":0.7,"summary":"x"}')
    result = svc._assess_with_retry("有点焦虑", [])
    assert result.risk == RiskLevel.MEDIUM


def test_score_upgrades_low_to_medium():
    svc = _service('{"emotion":"DEPRESSED","emotionScore":3.0,"risk":"LOW","confidence":0.6,"summary":"x"}')
    result = svc._assess_with_retry("心情不好", [])
    assert result.risk == RiskLevel.MEDIUM


def test_high_risk_emotion_forces_high():
    svc = _service('{"emotion":"HIGH_RISK","emotionScore":3.0,"risk":"MEDIUM","confidence":0.9,"summary":"danger"}')
    result = svc._assess_with_retry("我很危险", [])
    assert result.emotion == EmotionLabel.HIGH_RISK
    assert result.risk == RiskLevel.HIGH


# ---------------------------------------------------------------------------
# JSON parsing robustness
# ---------------------------------------------------------------------------

def test_json_extracted_from_surrounding_text():
    raw = 'Here is the result: {"emotion":"ANXIETY","emotionScore":2.0,"risk":"LOW","confidence":0.7,"summary":"ok"} done.'
    svc = _service(raw)
    result = svc._assess_with_retry("有点焦虑", [])
    assert result.emotion == EmotionLabel.ANXIETY
    assert result.risk == RiskLevel.LOW


# ---------------------------------------------------------------------------
# Schema validation failure -> retry -> raises (legacy path, issue 02)
# _assess_with_retry raises after retries exhausted; assess-level fallback
# is covered by the classifier path in test_classifier_assessment.py.
# ---------------------------------------------------------------------------

def test_schema_validation_error_retries_then_raises():
    # Missing required field 'summary' -> ValidationError -> retry -> raises
    svc = _service('{"emotion":"ANXIETY","emotionScore":2.0,"risk":"LOW","confidence":0.7}', max_retries=2)
    try:
        svc._assess_with_retry("有点焦虑", [])
        assert False, "Should have raised after retries exhausted"
    except Exception:
        pass
    assert svc.ai.call_count == 2


def test_invalid_json_retries_then_raises():
    svc = _service("not json at all", max_retries=2)
    try:
        svc._assess_with_retry("有点焦虑", [])
        assert False, "Should have raised"
    except Exception:
        pass
    assert svc.ai.call_count == 2


def test_transient_error_retries_then_raises():
    svc = _service(error_type="transient", max_retries=2)
    try:
        svc._assess_with_retry("有点焦虑", [])
        assert False, "Should have raised"
    except Exception:
        pass
    assert svc.ai.call_count == 2


def test_unexpected_error_retries_then_raises():
    svc = _service(error_type="runtime", max_retries=2)
    try:
        svc._assess_with_retry("有点焦虑", [])
        assert False, "Should have raised"
    except Exception:
        pass
    assert svc.ai.call_count == 2


# ---------------------------------------------------------------------------
# Permanent error -> no retry -> raises immediately (issue 02)
# ---------------------------------------------------------------------------

def test_permanent_error_raises_immediately():
    svc = _service(error_type="permanent", max_retries=3)
    try:
        svc._assess_with_retry("有点焦虑", [])
        assert False, "Should have raised"
    except PermanentAssessmentError:
        pass
    assert svc.ai.call_count == 1


# ---------------------------------------------------------------------------
# Safe fallback is conservative (issue 02)
# ---------------------------------------------------------------------------

def test_safe_fallback_is_medium_risk():
    fb = safe_fallback_assessment()
    assert fb.risk == RiskLevel.MEDIUM
    assert fb.emotion == EmotionLabel.ANXIETY
    assert fb.confidence <= 0.5


# ---------------------------------------------------------------------------
# Schema validation unit tests (issue 02)
# ---------------------------------------------------------------------------

def test_schema_accepts_valid_assessment():
    schema = RiskAssessmentSchema(
        emotion="ANXIETY", emotionScore=2.5, risk="LOW", confidence=0.7, summary="stress"
    )
    assert schema.emotion == EmotionLabel.ANXIETY
    assert schema.risk == RiskLevel.LOW


def test_schema_rejects_invalid_emotion():
    try:
        RiskAssessmentSchema(
            emotion="INVALID", emotionScore=2.0, risk="LOW", confidence=0.7, summary="x"
        )
        assert False, "Should have raised"
    except Exception:
        pass


def test_schema_rejects_out_of_range_score():
    try:
        RiskAssessmentSchema(
            emotion="ANXIETY", emotionScore=99.0, risk="LOW", confidence=0.7, summary="x"
        )
        assert False, "Should have raised"
    except Exception:
        pass


def test_schema_rejects_missing_summary():
    try:
        RiskAssessmentSchema(
            emotion="ANXIETY", emotionScore=2.0, risk="LOW", confidence=0.7
        )
        assert False, "Should have raised"
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Async variant (legacy _aassess_with_retry path)
# ---------------------------------------------------------------------------

def test_async_assess_normal():
    import asyncio
    svc = _service('{"emotion":"ANXIETY","emotionScore":2.5,"risk":"LOW","confidence":0.72,"summary":"stress"}')
    result = asyncio.run(svc._aassess_with_retry("最近考试压力很大", []))
    assert result.emotion == EmotionLabel.ANXIETY
    assert result.risk == RiskLevel.LOW


def test_async_assess_transient_raises():
    import asyncio
    svc = _service(error_type="transient", max_retries=2)
    try:
        asyncio.run(svc._aassess_with_retry("有点焦虑", []))
        assert False, "Should have raised"
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def test_score_for_emotion():
    assert score_for_emotion(EmotionLabel.HIGH_RISK) == 4.0
    assert score_for_emotion(EmotionLabel.DEPRESSED) == 3.0
    assert score_for_emotion(EmotionLabel.ANXIETY) == 2.0
    assert score_for_emotion(EmotionLabel.NORMAL) == 0.0


def test_risk_from_score():
    assert risk_from_score(4.0) == RiskLevel.HIGH
    assert risk_from_score(5.0) == RiskLevel.HIGH
    assert risk_from_score(3.0) == RiskLevel.MEDIUM
    assert risk_from_score(3.5) == RiskLevel.MEDIUM
    assert risk_from_score(2.9) == RiskLevel.LOW
    assert risk_from_score(0.0) == RiskLevel.LOW


def test_risk_order():
    assert risk_order(RiskLevel.LOW) == 1
    assert risk_order(RiskLevel.MEDIUM) == 2
    assert risk_order(RiskLevel.HIGH) == 3


# ---------------------------------------------------------------------------
# Heuristic still available for keyword-based fallback
# ---------------------------------------------------------------------------

def test_heuristic_depressed():
    result = heuristic("最近很抑郁，一直低落")
    assert result.emotion == EmotionLabel.DEPRESSED
    assert result.risk == RiskLevel.MEDIUM


def test_heuristic_anxiety():
    result = heuristic("最近压力大很焦虑")
    assert result.emotion == EmotionLabel.ANXIETY
    assert result.risk == RiskLevel.LOW


def test_heuristic_normal():
    result = heuristic("今天天气不错")
    assert result.emotion == EmotionLabel.NORMAL
    assert result.risk == RiskLevel.LOW


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
