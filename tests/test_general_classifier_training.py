from finetune.scripts.train_general_classifier import LABELS, metrics, stratified_sample


def test_classifier_metrics_use_ground_truth_and_report_high_risk_recall() -> None:
    rows = [{"output": label} for label in LABELS]
    report = metrics(rows, list(LABELS))
    assert report["accuracy"] == 1.0
    assert report["macroF1"] == 1.0
    assert report["highRiskRecall"] == 1.0
    assert report["outputValidity"] == 1.0


def test_classifier_metrics_count_invalid_generated_output() -> None:
    rows = [{"output": label} for label in LABELS]
    report = metrics(rows, ["正常", "焦虑", "低落", "这不是一个标签"])
    assert report["outputValidity"] == 0.75
    assert report["confusionMatrix"]["高风险"]["__INVALID__"] == 1
    assert report["highRiskRecall"] == 0.0


def test_small_sample_is_seeded_and_label_stratified() -> None:
    rows = [
        {"id": f"{label}-{index}", "output": label}
        for label in LABELS
        for index in range(5)
    ]
    first = stratified_sample(rows, 8, 42)
    second = stratified_sample(rows, 8, 42)
    assert first == second
    assert {row["output"] for row in first} == set(LABELS)
