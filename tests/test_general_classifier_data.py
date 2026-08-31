from finetune.scripts.generate_general_classifier_data import build_rows, validate


def test_general_classifier_data_is_grouped_balanced_and_leak_free() -> None:
    report = validate(build_rows())
    assert report["totalCases"] == 360
    assert report["sourceGroupLeakage"] == 0
    assert report["crossSplitNearDuplicates"] == 0
    assert report["humanReviewComplete"] is False
    assert report["distribution"]["train"]["cases"] == 200
    assert report["distribution"]["val"]["cases"] == 80
    assert report["distribution"]["test"]["cases"] == 80
    for split in ("train", "val", "test"):
        assert set(report["distribution"][split]["labels"]) == {"正常", "焦虑", "低落", "高风险"}
        assert len(report["distribution"][split]["scenarios"]) == 10
