"""Tests for the before/after comparison script.

Run: python -m pytest tests/test_compare_eval.py
"""
from __future__ import annotations

from finetune.scripts.compare_eval import compare


def _report(acc, f1, hr_recall, per_class_f1=0.8):
    return {
        "model": "test",
        "provider": "mock",
        "accuracy": acc,
        "macroF1": f1,
        "highRiskRecall": hr_recall,
        "perClass": {cls: {"precision": 0.8, "recall": 0.8, "f1": per_class_f1} for cls in ["正常", "焦虑", "低落", "高风险"]},
    }


def test_compare_records_delta():
    before = _report(0.70, 0.68, 0.85)
    after = _report(0.85, 0.83, 0.95)
    result = compare(before, after)
    assert abs(result["delta"]["accuracy"] - 0.15) < 1e-9
    assert abs(result["delta"]["macroF1"] - 0.15) < 1e-9
    assert abs(result["delta"]["highRiskRecall"] - 0.10) < 1e-9


def test_high_risk_recall_gate_passes_when_within_threshold():
    before = _report(0.70, 0.68, 0.90)
    after = _report(0.85, 0.83, 0.88)
    result = compare(before, after)
    assert result["highRiskRecallGate"] is True


def test_high_risk_recall_gate_fails_when_significant_drop():
    before = _report(0.70, 0.68, 0.90)
    after = _report(0.85, 0.83, 0.80)
    result = compare(before, after)
    assert result["highRiskRecallGate"] is False


def test_compare_preserves_per_class():
    before = _report(0.70, 0.68, 0.85, per_class_f1=0.7)
    after = _report(0.85, 0.83, 0.95, per_class_f1=0.9)
    result = compare(before, after)
    assert result["beforePerClass"]["焦虑"]["f1"] == 0.7
    assert result["afterPerClass"]["焦虑"]["f1"] == 0.9
