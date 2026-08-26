"""Tests for the classifier evaluation runner.

Ticket 03: verifies normalize_label, classify_mock and compute_metrics.
Run:  python tests/test_cls_eval.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.cls_eval.runner import CLASSES, classify_mock, compute_metrics, normalize_label


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


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    passed = 0
    failed = 0
    for test in _TESTS:
        try:
            test()
            print(f"  PASS  {test.__name__}")
            passed += 1
        except AssertionError as exc:
            print(f"  FAIL  {test.__name__}: {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERROR {test.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(_TESTS)} total")
    sys.exit(1 if failed else 0)
