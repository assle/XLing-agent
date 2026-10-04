"""Tests for direct safety checks, conservative fallback and risk-score rules."""
from __future__ import annotations

from app.core.enums import EmotionLabel, RiskLevel
from app.services.assessment import (
    PsychologicalAssessmentService,
    risk_from_score,
    safe_fallback_assessment,
    score_for_emotion,
)


class FakeAiClient:
    def __init__(self, label: str):
        self.label = label
        self.call_count = 0

    def classify(self, text: str) -> str:
        self.call_count += 1
        return self.label


def _service(label: str) -> PsychologicalAssessmentService:
    return PsychologicalAssessmentService(FakeAiClient(label))


def test_high_risk_keyword_skips_llm_suicide():
    """即使分类器预设正常标签，也先输入明确自杀表达。

    检查直接判为高风险且分类调用次数为零。
    """
    svc = _service("正常")
    result = svc.assess("我不想活了，想自杀")
    assert result.emotion == EmotionLabel.HIGH_RISK
    assert result.risk == RiskLevel.HIGH
    assert svc.ai.call_count == 0


def test_high_risk_keyword_skips_llm_selfharm():
    """输入明确自伤表达并提供正常分类标签。

    检查规则优先返回高风险，不消耗模型调用。
    """
    svc = _service("正常")
    result = svc.assess("我想伤害自己")
    assert result.risk == RiskLevel.HIGH
    assert svc.ai.call_count == 0


def test_safe_fallback_is_medium_risk():
    """检查模型不可用时的固定备用评估。

    要求中风险、焦虑标签和较低置信度，避免把失败当作没有风险。
    """
    fb = safe_fallback_assessment()
    assert fb.risk == RiskLevel.MEDIUM
    assert fb.emotion == EmotionLabel.ANXIETY
    assert fb.confidence <= 0.5


def test_score_for_emotion():
    """逐类核对内部情绪分数映射。

    确认高风险、低落、焦虑和正常对应预设规则值。
    """
    assert score_for_emotion(EmotionLabel.HIGH_RISK) == 4.0
    assert score_for_emotion(EmotionLabel.DEPRESSED) == 3.0
    assert score_for_emotion(EmotionLabel.ANXIETY) == 2.0
    assert score_for_emotion(EmotionLabel.NORMAL) == 0.0


def test_risk_from_score():
    """检查三分和四分阈值及其相邻区间。

    验证等于边界时进入对应较高等级。
    """
    assert risk_from_score(4.0) == RiskLevel.HIGH
    assert risk_from_score(5.0) == RiskLevel.HIGH
    assert risk_from_score(3.0) == RiskLevel.MEDIUM
    assert risk_from_score(3.5) == RiskLevel.MEDIUM
    assert risk_from_score(2.9) == RiskLevel.LOW
    assert risk_from_score(0.0) == RiskLevel.LOW
