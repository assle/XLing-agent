"""Tests for the fine-tuned classifier path in PsychologicalAssessmentService.

Ticket 02: classifier replaces the second risk-assessment layer. The classifier
outputs only a Chinese emotion label; the service maps it to EmotionLabel and
fills score/risk/confidence/summary via rules. Keyword short-circuit (layer 1)
and conservative fallback (layer 3) remain unchanged.

Run: python -m pytest tests/test_classifier_assessment.py
"""
from __future__ import annotations

import asyncio

from app.core.enums import EmotionLabel, RiskLevel
from app.services.assessment import (
    PsychologicalAssessmentService,
    assessment_from_label,
    label_to_emotion,
)


class FakeClassifierAiClient:
    """Test double with classifier support plus legacy complete for back-compat."""

    def __init__(self, label: str = "", error: bool = False):
        self._label = label
        self._error = error
        self.classify_count = 0
        self.complete_count = 0

    def classify(self, text: str) -> str:
        self.classify_count += 1
        if self._error:
            raise RuntimeError("simulated classifier outage")
        return self._label

    async def aclassify(self, text: str) -> str:
        return self.classify(text)

    def complete(self, messages):  # noqa: ANN001
        self.complete_count += 1
        return '{"emotion":"NORMAL","emotionScore":0.0,"risk":"LOW","confidence":0.5,"summary":"legacy"}'

    async def acomplete(self, messages):  # noqa: ANN001
        return self.complete(messages)


def _svc(label: str = "", error: bool = False) -> PsychologicalAssessmentService:
    return PsychologicalAssessmentService(FakeClassifierAiClient(label, error))


# ---------------------------------------------------------------------------
# Label mapping unit tests
# ---------------------------------------------------------------------------

def test_label_to_emotion_maps_all_four():
    assert label_to_emotion("正常") == EmotionLabel.NORMAL
    assert label_to_emotion("焦虑") == EmotionLabel.ANXIETY
    assert label_to_emotion("低落") == EmotionLabel.DEPRESSED
    assert label_to_emotion("高风险") == EmotionLabel.HIGH_RISK


def test_label_to_emotion_unknown_returns_none():
    assert label_to_emotion("不知道") is None
    assert label_to_emotion("") is None


def test_assessment_from_label_anxiety():
    a = assessment_from_label("焦虑")
    assert a.emotion == EmotionLabel.ANXIETY
    assert a.emotion_score == 2.0
    assert a.risk == RiskLevel.LOW
    assert a.confidence > 0.5
    assert "焦虑" in a.summary


def test_assessment_from_label_depressed():
    a = assessment_from_label("低落")
    assert a.emotion == EmotionLabel.DEPRESSED
    assert a.emotion_score == 3.0
    assert a.risk == RiskLevel.MEDIUM


def test_assessment_from_label_high_risk():
    a = assessment_from_label("高风险")
    assert a.emotion == EmotionLabel.HIGH_RISK
    assert a.emotion_score == 4.0
    assert a.risk == RiskLevel.HIGH


def test_assessment_from_label_normal():
    a = assessment_from_label("正常")
    assert a.emotion == EmotionLabel.NORMAL
    assert a.emotion_score == 0.0
    assert a.risk == RiskLevel.LOW


# ---------------------------------------------------------------------------
# Layer 1: high-risk keyword short-circuit unchanged
# ---------------------------------------------------------------------------

def test_keyword_skips_classifier_suicide():
    svc = _svc(label="正常")
    result = svc.assess("我不想活了，想自杀")
    assert result.risk == RiskLevel.HIGH
    assert result.emotion == EmotionLabel.HIGH_RISK
    assert svc.ai.classify_count == 0


def test_keyword_skips_classifier_selfharm():
    svc = _svc(label="正常")
    result = svc.assess("我想伤害自己")
    assert result.risk == RiskLevel.HIGH
    assert svc.ai.classify_count == 0


# ---------------------------------------------------------------------------
# Layer 2: classifier path -> label -> rule-mapped assessment
# ---------------------------------------------------------------------------

def test_classifier_anxiety_maps_low():
    result = _svc(label="焦虑").assess("最近考试压力很大")
    assert result.emotion == EmotionLabel.ANXIETY
    assert result.risk == RiskLevel.LOW
    assert result.emotion_score == 2.0


def test_classifier_depressed_maps_medium():
    result = _svc(label="低落").assess("最近一直很低落")
    assert result.emotion == EmotionLabel.DEPRESSED
    assert result.risk == RiskLevel.MEDIUM
    assert result.emotion_score == 3.0


def test_classifier_high_risk_maps_high():
    result = _svc(label="高风险").assess("感觉很危险")
    assert result.emotion == EmotionLabel.HIGH_RISK
    assert result.risk == RiskLevel.HIGH
    assert result.emotion_score == 4.0


def test_classifier_normal_maps_low():
    result = _svc(label="正常").assess("今天天气不错")
    assert result.emotion == EmotionLabel.NORMAL
    assert result.risk == RiskLevel.LOW
    assert result.emotion_score == 0.0


def test_classifier_strips_whitespace_in_label():
    result = _svc(label="  焦虑\n").assess("有点紧张")
    assert result.emotion == EmotionLabel.ANXIETY


# ---------------------------------------------------------------------------
# Layer 3: classifier failure / invalid label -> conservative fallback
# ---------------------------------------------------------------------------

def test_classifier_outage_falls_back_to_medium():
    svc = _svc(error=True)
    result = svc.assess("有点担心")
    assert result.risk == RiskLevel.MEDIUM
    assert result.confidence <= 0.5


def test_classifier_invalid_label_falls_back_to_medium():
    svc = _svc(label="不知道")
    result = svc.assess("有点担心")
    assert result.risk == RiskLevel.MEDIUM


def test_classifier_empty_label_falls_back_to_medium():
    svc = _svc(label="")
    result = svc.assess("有点担心")
    assert result.risk == RiskLevel.MEDIUM


# ---------------------------------------------------------------------------
# Async variant
# ---------------------------------------------------------------------------

def test_async_classifier_anxiety():
    result = asyncio.run(_svc(label="焦虑").aassess("最近考试压力很大"))
    assert result.emotion == EmotionLabel.ANXIETY
    assert result.risk == RiskLevel.LOW


def test_async_classifier_outage_fallback():
    result = asyncio.run(_svc(error=True).aassess("有点担心"))
    assert result.risk == RiskLevel.MEDIUM


def test_async_keyword_skips_classifier():
    svc = _svc(label="正常")
    result = asyncio.run(svc.aassess("我想自杀"))
    assert result.risk == RiskLevel.HIGH
    assert svc.ai.classify_count == 0


# ---------------------------------------------------------------------------
# Legacy general-LLM path retained (complete still works for fallback use)
# ---------------------------------------------------------------------------

def test_legacy_complete_still_callable():
    """The old general-LLM JSON path is retained in code even if assess no
    longer uses it by default."""
    svc = _svc()
    raw = svc.ai.complete([])
    assert "emotion" in raw


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
