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
    """传入精确的合法中文标签。

    检查标签保持不变。
    """
    assert normalize_label("焦虑") == "焦虑"


def test_normalize_label_with_whitespace():
    """在标签前后加入空白和换行。

    检查仍能规范成合法标签。
    """
    assert normalize_label("  低落\n") == "低落"


def test_normalize_label_with_extra_text():
    """在说明句中夹入合法标签。

    检查保留非规范输出，避免把业务会降级的解释句误算为合法标签。
    """
    assert normalize_label("根据分析，标签是：高风险") == "根据分析，标签是：高风险"


def test_normalize_label_rejects_multiple_labels():
    assert normalize_label("正常、焦虑、低落、高风险") not in CLASSES


def test_normalize_label_no_match_returns_stripped():
    """输入不含已知标签的文本。

    检查保留去空白后的原文，而非猜测类别。
    """
    assert normalize_label("不清楚") == "不清楚"


# ---------------------------------------------------------------------------
# classify_mock
# ---------------------------------------------------------------------------

def test_classify_mock_high_risk():
    """给模拟分类器输入明确风险词。

    检查固定规则优先返回高风险。
    """
    assert classify_mock("我不想活了，想自杀") == "高风险"


def test_classify_mock_depressed():
    """输入低落相关关键词。

    检查模拟规则返回低落标签。
    """
    assert classify_mock("最近很抑郁，一直低落") == "低落"


def test_classify_mock_anxiety():
    """输入压力和焦虑表达。

    检查模拟分类器返回焦虑。
    """
    assert classify_mock("考研压力很大，很焦虑") == "焦虑"


def test_classify_mock_normal():
    """输入普通天气表达。

    检查没有匹配支持或风险词时返回正常。
    """
    assert classify_mock("今天天气不错") == "正常"


# ---------------------------------------------------------------------------
# compute_metrics
# ---------------------------------------------------------------------------

def _case(exp: str, pred: str) -> dict:
    """创建只包含真实标签、预测标签和命中标志的评估样本。

    供指标测试直接构造可手算的分类结果。
    """
    return {"expected": exp, "predicted": pred, "hit": exp == pred}


def test_compute_metrics_perfect():
    """为每类准备全部预测正确的样本。

    检查准确率、类别平均综合分数和高风险召回均为一。
    """
    results = [_case(c, c) for c in CLASSES for _ in range(10)]
    m = compute_metrics(results)
    assert m["accuracy"] == 1.0
    assert m["macroF1"] == 1.0
    assert m["highRiskRecall"] == 1.0


def test_compute_metrics_all_wrong():
    """把所有正常样本都预测成焦虑。

    检查准确率和正常类召回为零。
    """
    results = [_case("正常", "焦虑") for _ in range(10)]
    m = compute_metrics(results)
    assert m["accuracy"] == 0.0
    assert m["perClass"]["正常"]["recall"] == 0.0


def test_compute_metrics_high_risk_recall():
    # 10 high-risk cases, 8 predicted correctly
    """十条真实高风险中正确识别八条。

    检查独立高风险召回及分类别召回均为 0.8。
    """
    results = [_case("高风险", "高风险") for _ in range(8)] + [_case("高风险", "低落") for _ in range(2)]
    m = compute_metrics(results)
    assert m["highRiskRecall"] == 0.8
    assert m["perClass"]["高风险"]["recall"] == 0.8


def test_compute_metrics_invalid_high_risk_output_counts_as_missed_risk():
    metrics = compute_metrics([_case("高风险", "高风险"), _case("高风险", "__ERROR__")])
    assert metrics["highRiskRecall"] == 0.5
    assert metrics["accuracy"] == 0.5


def test_compute_metrics_confusion_matrix_shape():
    """把一条焦虑样本预测成低落。

    检查计数落在真实类别为焦虑、预测列为低落的位置。
    """
    results = [_case("焦虑", "低落")]
    m = compute_metrics(results)
    assert m["confusionMatrix"]["焦虑"]["低落"] == 1
    assert m["confusionMatrix"]["焦虑"]["焦虑"] == 0


def test_compute_metrics_total_cases():
    """给四类各准备一条样本。

    检查总体样本数为四。
    """
    results = [_case(c, c) for c in CLASSES]
    m = compute_metrics(results)
    assert m["totalCases"] == 4


def test_classifier_eval_report_contains_artifact_version(tmp_path):
    """在临时文件中运行单条模拟分类评估。

    检查报告包含数据集指纹和分类模型名称。
    """
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
