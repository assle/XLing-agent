from __future__ import annotations

import json

from evals.config import EvalSettings
from evals.risk.calibration_runner import collect_calibration_inputs, evaluate_calibration


def test_calibration_eval_writes_model_artifact_and_uncertainty_metrics(tmp_path):
    calibration = [
        {"id": "c-low-1", "expectedRisk": "LOW", "logits": [5.0, 0.0, -2.0]},
        {"id": "c-low-2", "expectedRisk": "LOW", "logits": [3.0, 1.0, -1.0]},
        {"id": "c-med-1", "expectedRisk": "MEDIUM", "logits": [0.0, 5.0, -1.0]},
        {"id": "c-med-2", "expectedRisk": "MEDIUM", "logits": [1.0, 3.0, 0.0]},
        {"id": "c-high-1", "expectedRisk": "HIGH", "logits": [-2.0, 0.0, 5.0]},
        {"id": "c-high-2", "expectedRisk": "HIGH", "logits": [-1.0, 1.0, 3.0]},
    ]
    test = [
        {"id": "t-low", "expectedRisk": "LOW", "logits": [4.0, 0.0, -2.0]},
        {"id": "t-med", "expectedRisk": "MEDIUM", "logits": [0.0, 4.0, -1.0]},
        {"id": "t-high", "expectedRisk": "HIGH", "logits": [-2.0, 0.0, 4.0]},
        {"id": "t-ambiguous", "expectedRisk": "HIGH", "logits": [0.0, 0.0, 0.0]},
    ]
    calibration_path = tmp_path / "calibration.json"
    test_path = tmp_path / "test.json"
    calibration_path.write_text(json.dumps(calibration), encoding="utf-8")
    test_path.write_text(json.dumps(test), encoding="utf-8")
    settings = EvalSettings(
        risk_calibration_dataset=str(calibration_path),
        risk_calibration_test_dataset=str(test_path),
        risk_calibration_output=str(tmp_path / "report.json"),
        risk_calibration_artifact_output=str(tmp_path / "artifact.json"),
        risk_conformal_alpha=0.2,
    )

    report = evaluate_calibration(settings)

    assert report["calibrationCases"] == 6
    assert report["testCases"] == 4
    assert set(report["raw"]) >= {"accuracy", "highRiskRecall", "ece", "brier"}
    assert set(report["calibrated"]) >= {"accuracy", "highRiskRecall", "ece", "brier"}
    assert set(report["conformal"]) >= {"coverage", "averageSetSize", "reviewRate"}
    assert 0.0 <= report["conformal"]["coverage"] <= 1.0
    assert isinstance(report["deployable"], bool)
    assert report["decision"] in {"enable-calibrated-risk", "keep-current-risk-path"}
    artifact = json.loads((tmp_path / "artifact.json").read_text(encoding="utf-8"))
    assert artifact["calibrationVersion"] == report["calibrationVersion"]
    assert (tmp_path / "report.json").exists()


def test_calibration_split_is_disjoint_and_score_collection_is_reproducible(tmp_path):
    source = [
        {"id": "low-a", "text": "今天状态不错", "expected_risk": "LOW"},
        {"id": "mid-a", "text": "最近很低落", "expected_risk": "MEDIUM"},
        {"id": "high-a", "text": "我不想活了", "expected_risk": "HIGH"},
        {"id": "low-b", "text": "普通学习问题", "expected_risk": "LOW"},
        {"id": "mid-b", "text": "最近很抑郁", "expected_risk": "MEDIUM"},
        {"id": "high-b", "text": "我想自杀", "expected_risk": "HIGH"},
    ]
    split = {
        "calibration": ["low-a", "mid-a", "high-a"],
        "test": ["low-b", "mid-b", "high-b"],
    }
    source_path = tmp_path / "source.json"
    split_path = tmp_path / "split.json"
    source_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    split_path.write_text(json.dumps(split), encoding="utf-8")
    settings = EvalSettings(
        ai_provider="mock",
        risk_eval_ai_provider="mock",
        risk_calibration_source_dataset=str(source_path),
        risk_calibration_split=str(split_path),
        risk_calibration_dataset=str(tmp_path / "calibration.json"),
        risk_calibration_test_dataset=str(tmp_path / "test.json"),
    )

    calibration, test = collect_calibration_inputs(settings)

    assert {row["id"] for row in calibration}.isdisjoint(row["id"] for row in test)
    assert {row["expectedRisk"] for row in calibration} == {"LOW", "MEDIUM", "HIGH"}
    assert all(len(row["logits"]) == 3 for row in [*calibration, *test])
