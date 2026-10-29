from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from finetune.scripts.ablate_hf_classifier import ARMS, new_pair, observe_pair, paired_summary


class AdapterModel:
    enabled = True
    merged_adapters = []

    @contextmanager
    def disable_adapter(self):
        self.enabled = False
        try:
            yield
        finally:
            self.enabled = True

    def get_model_status(self):
        return SimpleNamespace(enabled=self.enabled, merged_adapters=self.merged_adapters)


def result(predicted):
    return {"status": "completed", "predicted": predicted, "strictValid": predicted != "__INVALID__",
            "latencyMs": 1, "invalidReason": "non_label_output" if predicted == "__INVALID__" else None}


def test_pair_toggles_only_selected_adapter_and_restores_state():
    model = AdapterModel()
    pair = new_pair({"id": "one", "datasetSHA256": "frozen", "input": "same question", "expected": "正常"})
    inputs = []

    def generate(text):
        inputs.append(text)
        return result("正常" if model.enabled else "焦虑")

    observe_pair(model, pair, generate)
    assert inputs == ["same question", "same question"]
    assert pair["arms"][ARMS[0]]["adapterEnabled"] is False
    assert pair["arms"][ARMS[1]]["adapterEnabled"] is True
    assert pair["arms"][ARMS[0]]["predicted"] == "焦虑"
    assert pair["arms"][ARMS[1]]["predicted"] == "正常"
    assert model.enabled is True


def test_merged_adapters_cannot_be_used_as_an_ablation():
    model = AdapterModel()
    model.merged_adapters = ["default"]
    pair = new_pair({"id": "one", "datasetSHA256": "frozen", "input": "question"})
    with pytest.raises(ValueError, match="Unmerged adapter"):
        observe_pair(model, pair, lambda text: pytest.fail("Must reject before inference"))
    assert not pair["arms"] and model.enabled is True


def test_failure_keeps_completed_first_arm_and_restores_adapter():
    model = AdapterModel()
    pair = new_pair({"id": "one", "datasetSHA256": "frozen", "input": "question"})
    calls = []

    def generate(text):
        calls.append(text)
        if len(calls) == 2:
            raise RuntimeError("Synthetic contract-test failure")
        return result("正常")

    with pytest.raises(RuntimeError):
        observe_pair(model, pair, generate)
    assert sorted(arm["status"] for arm in pair["arms"].values()) == ["completed", "started"]
    assert model.enabled is True
    with pytest.raises(ValueError, match="Complete paired"):
        paired_summary([pair])


def test_paired_delta_counts_regressions_and_invalid_outputs_in_full_denominator():
    data = [("正常", "__INVALID__", "正常"), ("焦虑", "焦虑", "高风险"),
            ("高风险", "高风险", "高风险"), ("低落", "高风险", "正常")]
    pairs = [{"key": str(i), "expected": expected, "arms": {ARMS[0]: result(before), ARMS[1]: result(after)}}
             for i, (expected, before, after) in enumerate(data)]
    summary = paired_summary(pairs)
    assert summary["improved"] == ["0"] and summary["regressed"] == ["1"]
    assert summary["accuracyDeltaPercentagePoints"] == 0
    assert summary["metrics"][ARMS[0]]["accuracy"] == .5
    assert summary["metrics"][ARMS[0]]["outputValidity"] == .75
    assert summary["metrics"][ARMS[1]]["outputValidity"] == 1
    assert summary["commonValidCases"] == 3
    assert summary["commonValidAccuracy"][ARMS[0]] == 2 / 3
    assert summary["commonValidAccuracy"][ARMS[1]] == 1 / 3
