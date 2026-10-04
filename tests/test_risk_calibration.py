from __future__ import annotations

from app.core.enums import RiskLevel
from app.services.risk_calibration import (
    CalibratedRiskEngine,
    ClassConditionalConformalPredictor,
    TemperatureScaler,
)


def test_temperature_scaling_reduces_negative_log_likelihood():
    """准备含过度自信错误的原始分数。

    检查选出的温度降低平均损失，转换后的概率总和仍为一。
    """
    logits = [
        [8.0, 0.0, 0.0],
        [7.0, 0.0, 0.0],
        [0.0, 8.0, 0.0],
        [8.0, 0.0, 0.0],
        [0.0, 0.0, 8.0],
    ]
    labels = [0, 0, 1, 1, 2]
    scaler = TemperatureScaler.fit(logits, labels)

    assert scaler.temperature > 1.0
    assert scaler.negative_log_likelihood(logits, labels) < TemperatureScaler(1.0).negative_log_likelihood(
        logits, labels
    )
    assert abs(sum(scaler.probabilities([1.0, 2.0, 3.0])) - 1.0) < 1e-9


def test_class_conditional_conformal_prediction_returns_safe_label_sets():
    """用三类校准样本拟合候选集合阈值。

    清晰低风险应只有低等级，模糊概率不得排除高风险。
    """
    calibration_probabilities = [
        [0.90, 0.08, 0.02],
        [0.80, 0.15, 0.05],
        [0.10, 0.80, 0.10],
        [0.05, 0.75, 0.20],
        [0.02, 0.08, 0.90],
        [0.05, 0.15, 0.80],
    ]
    labels = [0, 0, 1, 1, 2, 2]
    predictor = ClassConditionalConformalPredictor.fit(
        calibration_probabilities,
        labels,
        alpha=0.2,
    )

    assert predictor.predict_set([0.85, 0.10, 0.05]) == (RiskLevel.LOW,)
    assert RiskLevel.HIGH in predictor.predict_set([0.34, 0.33, 0.33])


def test_calibrated_risk_engine_marks_ambiguous_or_high_predictions_for_review():
    """比较明确低风险、高风险及显式高风险覆盖三种预测。

    检查审核标志、高风险固定概率和校准版本输出。
    """
    scaler = TemperatureScaler(1.0)
    conformal = ClassConditionalConformalPredictor(
        alpha=0.1,
        thresholds={
            RiskLevel.LOW: 0.25,
            RiskLevel.MEDIUM: 0.25,
            RiskLevel.HIGH: 0.25,
        },
    )
    engine = CalibratedRiskEngine(scaler, conformal, model_version="risk-v1")

    low = engine.predict([4.0, 0.0, -3.0])
    high = engine.predict([-3.0, 0.0, 4.0])
    explicit = engine.predict([0.0, 0.0, 0.0], explicit_high_risk=True)

    assert low.prediction_set == (RiskLevel.LOW,)
    assert low.requires_review is False
    assert high.requires_review is True
    assert explicit.selected_risk == RiskLevel.HIGH
    assert explicit.probabilities[RiskLevel.HIGH.value] == 1.0
    assert explicit.raw_probabilities[RiskLevel.HIGH.value] == 1.0
    assert engine.to_dict()["calibrationVersion"] == engine.calibration_version
