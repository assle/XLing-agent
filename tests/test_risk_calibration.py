from __future__ import annotations

from app.core.config import Settings
from app.core.enums import RiskLevel
from app.services.ai import AiClient
from app.services.assessment import PsychologicalAssessmentService
from app.services.risk_calibration import (
    CalibratedRiskEngine,
    ClassConditionalConformalPredictor,
    TemperatureScaler,
)


def test_temperature_scaling_reduces_negative_log_likelihood():
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


def test_psychological_assessment_exposes_calibrated_probabilities_and_prediction_set():
    class FakeAi:
        def __init__(self):
            self.calls = 0

        async def aclassify_with_logits(self, text: str) -> tuple[str, list[float]]:
            self.calls += 1
            return "焦虑", [0.0, 0.0, 0.0]

    engine = CalibratedRiskEngine(
        TemperatureScaler(1.0),
        ClassConditionalConformalPredictor(
            alpha=0.1,
            thresholds={risk: 0.25 for risk in (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH)},
        ),
        model_version="risk-v1",
    )
    ai = FakeAi()
    service = PsychologicalAssessmentService(ai, calibrated_risk=engine)

    import asyncio

    assessment = asyncio.run(service.aassess("我最近有些焦虑"))

    assert assessment.risk == RiskLevel.HIGH
    assert assessment.uncertain is True
    assert assessment.requires_review is True
    assert assessment.prediction_set == (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH)
    assert set(assessment.risk_probabilities) == {"LOW", "MEDIUM", "HIGH"}
    assert set(assessment.raw_risk_probabilities) == {"LOW", "MEDIUM", "HIGH"}
    assert assessment.calibration_version == engine.calibration_version
    assert ai.calls == 1


def test_calibrated_assessment_keeps_conservative_fallback_when_classifier_fails():
    class FailingAi:
        async def aclassify_with_logits(self, text: str):
            raise OSError("classifier unavailable")

    engine = CalibratedRiskEngine(
        TemperatureScaler(1.0),
        ClassConditionalConformalPredictor(
            alpha=0.1,
            thresholds={risk: 0.25 for risk in RiskLevel},
        ),
        model_version="risk-v1",
    )
    service = PsychologicalAssessmentService(FailingAi(), calibrated_risk=engine)

    import asyncio

    assessment = asyncio.run(service.aassess("我最近有些焦虑"))

    assert assessment.risk == RiskLevel.MEDIUM
    assert assessment.requires_review is False


def test_classifier_score_endpoint_returns_real_three_class_logits(monkeypatch):
    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"label": "低落", "logits": [-0.4, 2.3, -1.2]}

    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs["json"]))
        return Response()

    monkeypatch.setattr("app.services.ai.httpx.post", post)
    client = AiClient(
        Settings(
            ai_provider="ollama",
            risk_score_endpoint="http://classifier.local/score",
        )
    )

    label, logits = client.classify_with_logits("最近很低落")

    assert label == "低落"
    assert logits == [-0.4, 2.3, -1.2]
    assert calls == [
        ("http://classifier.local/score", {"text": "最近很低落"})
    ]
