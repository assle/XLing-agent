"""Tests for risk eval runner: metrics calculation and mock end-to-end.

Issue 02: risk evaluation runner.

Covers:
  - Pure metric functions (per-class precision/recall/F1, macro-F1, confusion matrix)
  - End-to-end mock mode (no API key needed, verifies report structure)
  - Summary generation (compact, no per-case detail)

Run: python -m pytest tests/test_risk_eval.py
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from evals.config import EvalSettings
from evals.risk.runner import build_summary, compute_metrics, evaluate

# ---------------------------------------------------------------------------
# Pure metrics: compute_metrics
# ---------------------------------------------------------------------------

def test_compute_metrics_perfect():
    results = [
        {"expected_risk": "HIGH", "predicted_risk": "HIGH"},
        {"expected_risk": "MEDIUM", "predicted_risk": "MEDIUM"},
        {"expected_risk": "LOW", "predicted_risk": "LOW"},
    ]
    metrics = compute_metrics(results)
    assert metrics["accuracy"] == 1.0
    assert metrics["macroF1"] == 1.0
    for cls in ["LOW", "MEDIUM", "HIGH"]:
        assert metrics["perClass"][cls]["precision"] == 1.0
        assert metrics["perClass"][cls]["recall"] == 1.0
        assert metrics["perClass"][cls]["f1"] == 1.0


def test_compute_metrics_mixed():
    results = [
        {"expected_risk": "HIGH", "predicted_risk": "HIGH"},
        {"expected_risk": "HIGH", "predicted_risk": "LOW"},
        {"expected_risk": "LOW", "predicted_risk": "HIGH"},
        {"expected_risk": "LOW", "predicted_risk": "LOW"},
    ]
    metrics = compute_metrics(results)
    assert abs(metrics["perClass"]["HIGH"]["precision"] - 0.5) < 1e-6
    assert abs(metrics["perClass"]["HIGH"]["recall"] - 0.5) < 1e-6
    assert abs(metrics["perClass"]["HIGH"]["f1"] - 0.5) < 1e-6
    assert abs(metrics["perClass"]["LOW"]["recall"] - 0.5) < 1e-6
    assert metrics["accuracy"] == 0.5


def test_compute_metrics_confusion_matrix():
    results = [
        {"expected_risk": "HIGH", "predicted_risk": "HIGH"},
        {"expected_risk": "HIGH", "predicted_risk": "MEDIUM"},
        {"expected_risk": "MEDIUM", "predicted_risk": "LOW"},
    ]
    metrics = compute_metrics(results)
    cm = metrics["confusionMatrix"]
    assert cm["HIGH"]["HIGH"] == 1
    assert cm["HIGH"]["MEDIUM"] == 1
    assert cm["HIGH"]["LOW"] == 0
    assert cm["MEDIUM"]["LOW"] == 1
    assert cm["MEDIUM"]["HIGH"] == 0


def test_compute_metrics_empty_class():
    results = [
        {"expected_risk": "HIGH", "predicted_risk": "HIGH"},
        {"expected_risk": "LOW", "predicted_risk": "LOW"},
    ]
    metrics = compute_metrics(results)
    assert metrics["perClass"]["MEDIUM"]["precision"] == 0.0
    assert metrics["perClass"]["MEDIUM"]["recall"] == 0.0
    assert metrics["perClass"]["MEDIUM"]["f1"] == 0.0


def test_compute_metrics_all_wrong():
    results = [
        {"expected_risk": "HIGH", "predicted_risk": "LOW"},
        {"expected_risk": "LOW", "predicted_risk": "HIGH"},
    ]
    metrics = compute_metrics(results)
    assert metrics["accuracy"] == 0.0
    assert metrics["perClass"]["HIGH"]["precision"] == 0.0
    assert metrics["perClass"]["HIGH"]["recall"] == 0.0
    assert metrics["perClass"]["HIGH"]["f1"] == 0.0
    assert metrics["macroF1"] == 0.0


# ---------------------------------------------------------------------------
# build_summary
# ---------------------------------------------------------------------------

def test_build_summary_excludes_results():
    report = {
        "totalCases": 5,
        "accuracy": 0.8,
        "macroF1": 0.75,
        "perClass": {},
        "confusionMatrix": {},
        "results": [{"id": "x"}],
    }
    summary = build_summary(report)
    assert "results" not in summary
    assert summary["totalCases"] == 5


# ---------------------------------------------------------------------------
# End-to-end mock mode
# ---------------------------------------------------------------------------

def test_evaluate_mock_mode_structure():
    cases = [
        {"id": "test-01", "text": "我不想活了", "expected_risk": "HIGH", "category": "direct"},
        {"id": "test-02", "text": "最近考研压力好大", "expected_risk": "LOW", "category": "mild_anxiety"},
        {"id": "test-03", "text": "今天天气不错", "expected_risk": "LOW", "category": "normal_vent"},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        dataset_path = tmp_path / "dataset.json"
        dataset_path.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")

        settings = EvalSettings().model_copy(update={
            "risk_eval_dataset": str(dataset_path),
            "risk_eval_output": str(tmp_path / "report.json"),
            "risk_eval_summary_output": str(tmp_path / "summary.json"),
            "risk_eval_ai_provider": "mock",
        })

        report = evaluate(settings, provider="mock")

    assert report["totalCases"] == 3
    assert report["provider"] == "mock"
    assert report["artifactVersion"]["datasetVersion"]
    assert report["artifactVersion"]["promptVersion"]
    assert report["artifactVersion"]["indexVersion"]
    assert "perClass" in report
    assert "confusionMatrix" in report
    assert "macroF1" in report
    assert "accuracy" in report
    assert len(report["results"]) == 3
    for r in report["results"]:
        assert "expectedRisk" in r
        assert "predictedRisk" in r
        assert "hit" in r
        assert "emotion" in r
        assert "confidence" in r


def test_evaluate_mock_mode_writes_files():
    cases = [
        {"id": "test-01", "text": "我想自杀", "expected_risk": "HIGH", "category": "direct"},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        dataset_path = tmp_path / "dataset.json"
        dataset_path.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")

        report_path = tmp_path / "report.json"
        summary_path = tmp_path / "summary.json"
        settings = EvalSettings().model_copy(update={
            "risk_eval_dataset": str(dataset_path),
            "risk_eval_output": str(report_path),
            "risk_eval_summary_output": str(summary_path),
            "risk_eval_ai_provider": "mock",
        })

        evaluate(settings, provider="mock")

        assert report_path.exists()
        assert summary_path.exists()
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        assert "results" not in summary
        assert "macroF1" in summary


def test_evaluate_mock_mode_keyword_high():
    """Messages with HIGH_RISK_WORDS should be predicted HIGH in mock mode."""
    cases = [
        {"id": "test-01", "text": "我想自杀", "expected_risk": "HIGH", "category": "direct"},
        {"id": "test-02", "text": "我想伤害自己", "expected_risk": "HIGH", "category": "direct"},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        dataset_path = tmp_path / "dataset.json"
        dataset_path.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")

        settings = EvalSettings().model_copy(update={
            "risk_eval_dataset": str(dataset_path),
            "risk_eval_output": str(tmp_path / "report.json"),
            "risk_eval_summary_output": str(tmp_path / "summary.json"),
        })

        report = evaluate(settings, provider="mock")

    assert report["results"][0]["predictedRisk"] == "HIGH"
    assert report["results"][0]["hit"] is True
    assert report["results"][1]["predictedRisk"] == "HIGH"
    assert report["accuracy"] == 1.0


def test_evaluate_mock_mode_false_positive_trap():
    """A boundary trap: normal text containing HIGH_RISK_WORDS should be predicted HIGH (false positive)."""
    cases = [
        {"id": "trap-01", "text": "不想活了真是太累了这破代码", "expected_risk": "LOW", "category": "trap", "notes": "含'不想活'但语境是抱怨"},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        dataset_path = tmp_path / "dataset.json"
        dataset_path.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")

        settings = EvalSettings().model_copy(update={
            "risk_eval_dataset": str(dataset_path),
            "risk_eval_output": str(tmp_path / "report.json"),
            "risk_eval_summary_output": str(tmp_path / "summary.json"),
        })

        report = evaluate(settings, provider="mock")

    assert report["results"][0]["predictedRisk"] == "HIGH"
    assert report["results"][0]["hit"] is False
    assert report["accuracy"] == 0.0
    assert report["perClass"]["LOW"]["recall"] == 0.0
