"""Tests for the decision-head layered safety path in PsychologicalAssessmentService.

决策头后端（classifier_backend=decision_head）的分层规则：
高风险概率达阈值直接判定；灰区或低置信回退生成式复核；复核分歧按严重度就高；
任何一路失败不得解释为低风险。关键词直连与保守回退保持不变。

Run: python -m pytest tests/test_decision_head_assessment.py
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core.enums import EmotionLabel, RiskLevel
from app.services.assessment import PsychologicalAssessmentService
from app.services.decision_classifier import DecisionPrediction
from tests.test_classifier_assessment import FakeClassifierAiClient

LABELS = ("正常", "焦虑", "低落", "高风险")

THRESHOLDS = SimpleNamespace(
    classifier_high_risk_threshold=0.5,
    classifier_high_risk_review_threshold=0.15,
    classifier_low_confidence_threshold=0.70,
    classifier_margin_threshold=0.30,
)


@pytest.fixture(autouse=True)
def _threshold_settings(monkeypatch):
    """固定分层阈值，避免本机 .env 影响测试确定性。"""
    monkeypatch.setattr("app.services.assessment.get_settings", lambda: THRESHOLDS)


def make_prediction(probs: tuple[float, float, float, float]) -> DecisionPrediction:
    """按（正常、焦虑、低落、高风险）顺序构造决策头预测结果。"""
    best = max(range(len(LABELS)), key=lambda index: probs[index])
    ordered = sorted(probs, reverse=True)
    return DecisionPrediction(
        label=LABELS[best],
        confidence=probs[best],
        margin=ordered[0] - ordered[1],
        high_risk_probability=probs[3],
        probabilities={label: probs[index] for index, label in enumerate(LABELS)},
        model_version="fake-decision-head",
    )


class FakeDecisionClassifier:
    """返回预设概率或模拟故障的决策头替身，记录调用次数。"""

    def __init__(self, probs=(0.95, 0.02, 0.02, 0.01), error: bool = False):
        self._probs = probs
        self._error = error
        self.predict_count = 0

    def predict(self, text: str) -> DecisionPrediction:
        """不做真实推理，按配置返回预测或抛错。"""
        self.predict_count += 1
        if self._error:
            raise RuntimeError("simulated decision head outage")
        return make_prediction(self._probs)


def _svc(probs=(0.95, 0.02, 0.02, 0.01), decision_error=False, fallback_label="", fallback_error=False):
    """创建决策头后端评估服务，生成式复核由替身标签控制。"""
    ai = FakeClassifierAiClient(fallback_label, fallback_error)
    decision = FakeDecisionClassifier(probs, decision_error)
    return PsychologicalAssessmentService(ai, decision_classifier=decision, backend="decision_head")


# ---------------------------------------------------------------------------
# 直接判定：高置信与高风险达阈值都不触发复核。
# ---------------------------------------------------------------------------

def test_high_confidence_normal_skips_fallback():
    """高置信正常判定直接返回，不调用生成式复核，置信度来自概率。"""
    svc = _svc()
    result = svc.assess("今天天气不错")
    assert result.emotion == EmotionLabel.NORMAL
    assert result.risk == RiskLevel.LOW
    assert result.confidence == pytest.approx(0.95)
    assert result.model_version == "fake-decision-head"
    assert svc.ai.classify_count == 0


def test_high_risk_over_threshold_direct():
    """高风险概率 0.6 达到直接判定阈值，无需复核。"""
    svc = _svc(probs=(0.1, 0.15, 0.15, 0.6))
    result = svc.assess("感觉很危险")
    assert result.emotion == EmotionLabel.HIGH_RISK
    assert result.risk == RiskLevel.HIGH
    assert result.confidence == pytest.approx(0.6)
    assert svc.ai.classify_count == 0


# ---------------------------------------------------------------------------
# 灰区与低置信回退：一致采用、分歧就高。
# ---------------------------------------------------------------------------

def test_gray_zone_fallback_high_risk_wins():
    """灰区样本复核为高风险时按就高原则判高风险，置信度降权。"""
    svc = _svc(probs=(0.1, 0.45, 0.15, 0.3), fallback_label="高风险")
    result = svc.assess("最近状态很糟")
    assert result.emotion == EmotionLabel.HIGH_RISK
    assert result.risk == RiskLevel.HIGH
    assert result.confidence <= 0.5
    assert svc.ai.classify_count == 1


def test_gray_zone_fallback_disagree_keeps_more_conservative():
    """灰区样本复核为正常时保留严重度更高的焦虑标签。"""
    svc = _svc(probs=(0.1, 0.45, 0.15, 0.3), fallback_label="正常")
    result = svc.assess("下周答辩很紧张")
    assert result.emotion == EmotionLabel.ANXIETY
    assert svc.ai.classify_count == 1


def test_low_confidence_fallback_agrees():
    """低置信样本与复核一致时采用该标签，置信度保留决策头概率。"""
    svc = _svc(probs=(0.3, 0.5, 0.15, 0.05), fallback_label="焦虑")
    result = svc.assess("最近考试压力很大")
    assert result.emotion == EmotionLabel.ANXIETY
    assert result.confidence == pytest.approx(0.5)
    assert svc.ai.classify_count == 1


def test_low_confidence_fallback_disagree_goes_conservative():
    """低置信样本复核分歧时就高：决策头正常、复核低落，取低落。"""
    svc = _svc(probs=(0.5, 0.2, 0.25, 0.05), fallback_label="低落")
    result = svc.assess("最近提不起劲")
    assert result.emotion == EmotionLabel.DEPRESSED
    assert result.risk == RiskLevel.MEDIUM
    assert result.confidence <= 0.5


# ---------------------------------------------------------------------------
# 失败路径：复核不可用时灰区保守高风险，其余保守中风险。
# ---------------------------------------------------------------------------

def test_gray_zone_fallback_error_conservative_high():
    """灰区样本复核服务故障时按保守高风险处理，不解释为低风险。"""
    svc = _svc(probs=(0.4, 0.25, 0.15, 0.2), fallback_error=True)
    result = svc.assess("有点撑不住")
    assert result.emotion == EmotionLabel.HIGH_RISK
    assert result.risk == RiskLevel.HIGH


def test_gray_zone_fallback_invalid_label_conservative_high():
    """灰区样本复核返回无效标签时同样按保守高风险处理。"""
    svc = _svc(probs=(0.4, 0.25, 0.15, 0.2), fallback_label="不知道")
    result = svc.assess("有点撑不住")
    assert result.emotion == EmotionLabel.HIGH_RISK


def test_low_confidence_fallback_error_safe_fallback():
    """非灰区低置信样本复核故障时沿用保守中风险评估。"""
    svc = _svc(probs=(0.3, 0.5, 0.15, 0.05), fallback_error=True)
    result = svc.assess("有点担心")
    assert result.risk == RiskLevel.MEDIUM
    assert result.confidence <= 0.5


def test_decision_head_error_uses_ollama_path():
    """决策头推理故障时退回生成式主路径，正常标签照常映射。"""
    svc = _svc(decision_error=True, fallback_label="正常")
    result = svc.assess("今天天气不错")
    assert result.emotion == EmotionLabel.NORMAL
    assert svc.ai.classify_count == 1


def test_both_backends_error_safe_fallback():
    """决策头与生成式都故障时返回保守中风险评估。"""
    svc = _svc(decision_error=True, fallback_error=True)
    result = svc.assess("有点担心")
    assert result.risk == RiskLevel.MEDIUM
    assert result.confidence <= 0.5


# ---------------------------------------------------------------------------
# 关键词直连与异步入口。
# ---------------------------------------------------------------------------

def test_keyword_skips_decision_head():
    """明确高风险表达直接高风险，决策头与复核都不调用。"""
    svc = _svc()
    result = svc.assess("我不想活了，想自杀")
    assert result.risk == RiskLevel.HIGH
    assert svc._decision_classifier.predict_count == 0
    assert svc.ai.classify_count == 0


def test_async_direct_and_fallback_paths():
    """异步入口的高置信直判与低置信复核一致路径与同步一致。"""
    direct = asyncio.run(_svc().aassess("今天天气不错"))
    assert direct.emotion == EmotionLabel.NORMAL
    svc = _svc(probs=(0.3, 0.5, 0.15, 0.05), fallback_label="焦虑")
    reviewed = asyncio.run(svc.aassess("最近考试压力很大"))
    assert reviewed.emotion == EmotionLabel.ANXIETY
    assert svc.ai.classify_count == 1


def test_async_decision_head_error_uses_ollama_path():
    """异步决策头故障时同样退回生成式主路径。"""
    svc = _svc(decision_error=True, fallback_label="正常")
    result = asyncio.run(svc.aassess("今天天气不错"))
    assert result.emotion == EmotionLabel.NORMAL
