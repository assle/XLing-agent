from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

from app.core.enums import RiskLevel

RISK_LEVELS = (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH)


@dataclass(frozen=True)
class TemperatureScaler:
    temperature: float

    @classmethod
    def fit(
        cls,
        logits: list[list[float]],
        labels: list[int],
    ) -> TemperatureScaler:
        if not logits or len(logits) != len(labels):
            raise ValueError("校准 logits 与标签必须非空且数量一致")
        candidates = [math.exp(-3.0 + index * 7.0 / 300.0) for index in range(301)]
        return min(
            (cls(temperature) for temperature in candidates),
            key=lambda scaler: scaler.negative_log_likelihood(logits, labels),
        )

    def probabilities(self, logits: list[float]) -> list[float]:
        if self.temperature <= 0:
            raise ValueError("temperature 必须大于 0")
        scaled = [value / self.temperature for value in logits]
        offset = max(scaled)
        exponentials = [math.exp(value - offset) for value in scaled]
        denominator = sum(exponentials)
        return [value / denominator for value in exponentials]

    def negative_log_likelihood(
        self,
        logits: list[list[float]],
        labels: list[int],
    ) -> float:
        losses = []
        for row, label in zip(logits, labels, strict=True):
            probability = self.probabilities(row)[label]
            losses.append(-math.log(max(probability, 1e-12)))
        return sum(losses) / max(1, len(losses))


@dataclass(frozen=True)
class ClassConditionalConformalPredictor:
    alpha: float
    thresholds: dict[RiskLevel, float]

    @classmethod
    def fit(
        cls,
        probabilities: list[list[float]],
        labels: list[int],
        *,
        alpha: float,
    ) -> ClassConditionalConformalPredictor:
        if not 0 < alpha < 1:
            raise ValueError("alpha 必须在 0 和 1 之间")
        if not probabilities or len(probabilities) != len(labels):
            raise ValueError("校准概率与标签必须非空且数量一致")
        thresholds = {}
        for label_index, risk in enumerate(RISK_LEVELS):
            scores = sorted(
                1.0 - row[label_index]
                for row, expected in zip(probabilities, labels, strict=True)
                if expected == label_index
            )
            if not scores:
                raise ValueError(f"校准集缺少 {risk.value} 样本")
            rank = math.ceil((len(scores) + 1) * (1.0 - alpha))
            thresholds[risk] = scores[min(rank, len(scores)) - 1]
        return cls(alpha=alpha, thresholds=thresholds)

    def predict_set(self, probabilities: list[float]) -> tuple[RiskLevel, ...]:
        included = tuple(
            risk
            for index, risk in enumerate(RISK_LEVELS)
            if 1.0 - probabilities[index] <= self.thresholds[risk]
        )
        # An empty set means the input does not resemble any calibrated class.
        # Returning every class makes uncertainty explicit and preserves HIGH.
        return included or RISK_LEVELS


@dataclass(frozen=True)
class RiskPrediction:
    probabilities: dict[str, float]
    prediction_set: tuple[RiskLevel, ...]
    selected_risk: RiskLevel
    uncertain: bool
    requires_review: bool
    model_version: str
    calibration_version: str


class CalibratedRiskEngine:
    def __init__(
        self,
        scaler: TemperatureScaler,
        conformal: ClassConditionalConformalPredictor,
        *,
        model_version: str,
    ) -> None:
        self.scaler = scaler
        self.conformal = conformal
        self.model_version = model_version
        self.calibration_version = _calibration_version(scaler, conformal)

    def predict(
        self,
        logits: list[float],
        *,
        explicit_high_risk: bool = False,
    ) -> RiskPrediction:
        prediction_set: tuple[RiskLevel, ...]
        if explicit_high_risk:
            probabilities = [0.0, 0.0, 1.0]
            prediction_set = (RiskLevel.HIGH,)
        else:
            probabilities = self.scaler.probabilities(logits)
            prediction_set = self.conformal.predict_set(probabilities)
        selected_risk = max(prediction_set, key=RISK_LEVELS.index)
        uncertain = len(prediction_set) != 1
        return RiskPrediction(
            probabilities={
                risk.value: probabilities[index]
                for index, risk in enumerate(RISK_LEVELS)
            },
            prediction_set=prediction_set,
            selected_risk=selected_risk,
            uncertain=uncertain,
            requires_review=RiskLevel.HIGH in prediction_set,
            model_version=self.model_version,
            calibration_version=self.calibration_version,
        )

    def to_dict(self) -> dict:
        return {
            "temperature": self.scaler.temperature,
            "alpha": self.conformal.alpha,
            "thresholds": {
                risk.value: threshold
                for risk, threshold in self.conformal.thresholds.items()
            },
            "modelVersion": self.model_version,
            "calibrationVersion": self.calibration_version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> CalibratedRiskEngine:
        return cls(
            TemperatureScaler(float(data["temperature"])),
            ClassConditionalConformalPredictor(
                alpha=float(data["alpha"]),
                thresholds={
                    RiskLevel(risk): float(threshold)
                    for risk, threshold in data["thresholds"].items()
                },
            ),
            model_version=str(data["modelVersion"]),
        )


def _calibration_version(
    scaler: TemperatureScaler,
    conformal: ClassConditionalConformalPredictor,
) -> str:
    payload = {
        "temperature": scaler.temperature,
        "alpha": conformal.alpha,
        "thresholds": {
            risk.value: conformal.thresholds[risk]
            for risk in RISK_LEVELS
        },
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_calibrated_risk_engine(path: str | Path) -> CalibratedRiskEngine:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return CalibratedRiskEngine.from_dict(data)
