from __future__ import annotations

import json
from pathlib import Path

from app.core.enums import RiskLevel
from app.core.versioning import ArtifactVersionResolver
from app.services.ai import AiClient
from app.services.risk_calibration import (
    RISK_LEVELS,
    CalibratedRiskEngine,
    ClassConditionalConformalPredictor,
    TemperatureScaler,
)
from evals.config import EvalSettings, get_eval_settings


def collect_calibration_inputs(
    settings: EvalSettings | None = None,
) -> tuple[list[dict], list[dict]]:
    settings = settings or get_eval_settings()
    source_rows = json.loads(
        Path(settings.risk_calibration_source_dataset).read_text(encoding="utf-8")
    )
    split = json.loads(Path(settings.risk_calibration_split).read_text(encoding="utf-8"))
    calibration_ids = set(split["calibration"])
    test_ids = set(split["test"])
    if calibration_ids.intersection(test_ids):
        raise ValueError("校准集与最终测试集不得重叠")
    rows_by_id = {row["id"]: row for row in source_rows}
    missing = calibration_ids.union(test_ids) - rows_by_id.keys()
    if missing:
        raise ValueError(f"切分清单包含未知案例：{sorted(missing)}")
    ai_settings = settings.model_copy(update={"ai_provider": settings.risk_eval_ai_provider})
    ai = AiClient(ai_settings)

    def collect(ids: set[str]) -> list[dict]:
        return [
            {
                "id": row_id,
                "expectedRisk": rows_by_id[row_id]["expected_risk"],
                "logits": ai.risk_logits(rows_by_id[row_id]["text"]),
            }
            for row_id in sorted(ids)
        ]

    calibration = collect(calibration_ids)
    test = collect(test_ids)
    for rows, output in (
        (calibration, Path(settings.risk_calibration_dataset)),
        (test, Path(settings.risk_calibration_test_dataset)),
    ):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return calibration, test


def evaluate_calibration(settings: EvalSettings | None = None) -> dict:
    settings = settings or get_eval_settings()
    calibration_path = Path(settings.risk_calibration_dataset)
    test_path = Path(settings.risk_calibration_test_dataset)
    calibration_rows = json.loads(calibration_path.read_text(encoding="utf-8"))
    test_rows = json.loads(test_path.read_text(encoding="utf-8"))

    calibration_logits = [row["logits"] for row in calibration_rows]
    calibration_labels = [_label_index(row["expectedRisk"]) for row in calibration_rows]
    scaler = TemperatureScaler.fit(calibration_logits, calibration_labels)
    calibration_probabilities = [scaler.probabilities(row) for row in calibration_logits]
    conformal = ClassConditionalConformalPredictor.fit(
        calibration_probabilities,
        calibration_labels,
        alpha=settings.risk_conformal_alpha,
    )
    engine = CalibratedRiskEngine(
        scaler,
        conformal,
        model_version=settings.ollama_classifier_model,
    )

    test_logits = [row["logits"] for row in test_rows]
    test_labels = [_label_index(row["expectedRisk"]) for row in test_rows]
    raw_probabilities = [TemperatureScaler(1.0).probabilities(row) for row in test_logits]
    calibrated_probabilities = [scaler.probabilities(row) for row in test_logits]
    decisions = [engine.predict(row) for row in test_logits]
    raw_metrics = _probability_metrics(raw_probabilities, test_labels)
    calibrated_metrics = _probability_metrics(calibrated_probabilities, test_labels)
    conformal_metrics = _conformal_metrics(decisions, test_labels)
    ece_improvement = (
        (raw_metrics["ece"] - calibrated_metrics["ece"]) / raw_metrics["ece"]
        if raw_metrics["ece"] > 0
        else 0.0
    )
    target_coverage = 1.0 - settings.risk_conformal_alpha
    deployable = (
        calibrated_metrics["highRiskRecall"] >= raw_metrics["highRiskRecall"]
        and ece_improvement >= 0.20
        and abs(conformal_metrics["coverage"] - target_coverage) <= 0.03
    )

    artifact = engine.to_dict()
    artifact_path = Path(settings.risk_calibration_artifact_output)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")

    report = {
        "artifactVersion": ArtifactVersionResolver(settings).current(calibration_path).to_dict(),
        "calibrationVersion": engine.calibration_version,
        "calibrationCases": len(calibration_rows),
        "testCases": len(test_rows),
        "raw": raw_metrics,
        "calibrated": calibrated_metrics,
        "conformal": conformal_metrics,
        "eceRelativeImprovement": ece_improvement,
        "targetCoverage": target_coverage,
        "deployable": deployable,
        "decision": "enable-calibrated-risk" if deployable else "keep-current-risk-path",
        "cases": [
            {
                "id": row["id"],
                "expectedRisk": row["expectedRisk"],
                "probabilities": decision.probabilities,
                "predictionSet": [risk.value for risk in decision.prediction_set],
                "uncertain": decision.uncertain,
                "requiresReview": decision.requires_review,
            }
            for row, decision in zip(test_rows, decisions, strict=True)
        ],
    }
    output_path = Path(settings.risk_calibration_output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _probability_metrics(probabilities: list[list[float]], labels: list[int]) -> dict:
    predicted = [max(range(len(row)), key=row.__getitem__) for row in probabilities]
    accuracy = sum(pred == label for pred, label in zip(predicted, labels, strict=True)) / max(1, len(labels))
    high_index = RISK_LEVELS.index(RiskLevel.HIGH)
    high_total = sum(label == high_index for label in labels)
    high_hits = sum(
        pred == high_index and label == high_index
        for pred, label in zip(predicted, labels, strict=True)
    )
    return {
        "accuracy": accuracy,
        "highRiskRecall": high_hits / max(1, high_total),
        "ece": _expected_calibration_error(probabilities, labels),
        "brier": _brier_score(probabilities, labels),
    }


def _expected_calibration_error(
    probabilities: list[list[float]],
    labels: list[int],
    bins: int = 10,
) -> float:
    total = max(1, len(labels))
    error = 0.0
    for bin_index in range(bins):
        lower = bin_index / bins
        upper = (bin_index + 1) / bins
        members = []
        for row, label in zip(probabilities, labels, strict=True):
            predicted = max(range(len(row)), key=row.__getitem__)
            confidence = row[predicted]
            if lower < confidence <= upper or (bin_index == 0 and confidence == 0):
                members.append((confidence, predicted == label))
        if members:
            mean_confidence = sum(item[0] for item in members) / len(members)
            accuracy = sum(item[1] for item in members) / len(members)
            error += len(members) / total * abs(accuracy - mean_confidence)
    return error


def _brier_score(probabilities: list[list[float]], labels: list[int]) -> float:
    scores = []
    for row, label in zip(probabilities, labels, strict=True):
        scores.append(sum((probability - (index == label)) ** 2 for index, probability in enumerate(row)))
    return sum(scores) / max(1, len(scores))


def _conformal_metrics(decisions, labels: list[int]) -> dict:
    covered = 0
    set_size = 0
    reviews = 0
    for decision, label in zip(decisions, labels, strict=True):
        expected = RISK_LEVELS[label]
        covered += expected in decision.prediction_set
        set_size += len(decision.prediction_set)
        reviews += decision.requires_review
    total = max(1, len(labels))
    return {
        "coverage": covered / total,
        "averageSetSize": set_size / total,
        "reviewRate": reviews / total,
    }


def _label_index(value: str) -> int:
    return RISK_LEVELS.index(RiskLevel(value))


if __name__ == "__main__":
    result = evaluate_calibration()
    print(json.dumps({key: value for key, value in result.items() if key != "cases"}, ensure_ascii=False, indent=2))
