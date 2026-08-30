"""Tests for the classifier evaluation runner.

Ticket 03: verifies normalize_label, classify_mock and compute_metrics.
Run: python -m pytest tests/test_cls_eval.py
"""
from __future__ import annotations

import json

from evals.classifier.runner import (
    CLASSES,
    classify_mock,
    compute_metrics,
    evaluate,
    normalize_label,
)
from evals.config import EvalSettings

# ---------------------------------------------------------------------------
# normalize_label
# ---------------------------------------------------------------------------

def test_normalize_label_exact():
    assert normalize_label("焦虑") == "焦虑"


def test_normalize_label_with_whitespace():
    assert normalize_label("  低落\n") == "低落"


def test_normalize_label_with_extra_text():
    assert normalize_label("根据分析，标签是：高风险") == "高风险"


def test_normalize_label_no_match_returns_stripped():
    assert normalize_label("不清楚") == "不清楚"


# ---------------------------------------------------------------------------
# classify_mock
# ---------------------------------------------------------------------------

def test_classify_mock_high_risk():
    assert classify_mock("我不想活了，想自杀") == "高风险"


def test_classify_mock_depressed():
    assert classify_mock("最近很抑郁，一直低落") == "低落"


def test_classify_mock_anxiety():
    assert classify_mock("考研压力很大，很焦虑") == "焦虑"


def test_classify_mock_normal():
    assert classify_mock("今天天气不错") == "正常"


# ---------------------------------------------------------------------------
# compute_metrics
# ---------------------------------------------------------------------------

def _case(exp: str, pred: str) -> dict:
    return {"expected": exp, "predicted": pred, "hit": exp == pred}


def test_compute_metrics_perfect():
    results = [_case(c, c) for c in CLASSES for _ in range(10)]
    m = compute_metrics(results)
    assert m["accuracy"] == 1.0
    assert m["macroF1"] == 1.0
    assert m["highRiskRecall"] == 1.0


def test_compute_metrics_all_wrong():
    results = [_case("正常", "焦虑") for _ in range(10)]
    m = compute_metrics(results)
    assert m["accuracy"] == 0.0
    assert m["perClass"]["正常"]["recall"] == 0.0


def test_compute_metrics_high_risk_recall():
    # 10 high-risk cases, 8 predicted correctly
    results = [_case("高风险", "高风险") for _ in range(8)] + [_case("高风险", "低落") for _ in range(2)]
    m = compute_metrics(results)
    assert m["highRiskRecall"] == 0.8
    assert m["perClass"]["高风险"]["recall"] == 0.8


def test_compute_metrics_confusion_matrix_shape():
    results = [_case("焦虑", "低落")]
    m = compute_metrics(results)
    assert m["confusionMatrix"]["焦虑"]["低落"] == 1
    assert m["confusionMatrix"]["焦虑"]["焦虑"] == 0


def test_compute_metrics_total_cases():
    results = [_case(c, c) for c in CLASSES]
    m = compute_metrics(results)
    assert m["totalCases"] == 4


def test_classifier_eval_report_contains_artifact_version(tmp_path):
    dataset = tmp_path / "classifier.jsonl"
    dataset.write_text(
        json.dumps({"input": "最近很焦虑", "output": "焦虑"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    settings = EvalSettings(
        cls_eval_dataset=str(dataset),
        cls_eval_output=str(tmp_path / "report.json"),
        cls_eval_summary_output=str(tmp_path / "summary.json"),
        cls_eval_ai_provider="mock",
    )

    report = evaluate(settings)

    assert report["artifactVersion"]["datasetVersion"]
    assert report["artifactVersion"]["classifierModel"]
