from types import SimpleNamespace

import pytest
import torch

from finetune.scripts import train_general_classifier as training
from finetune.scripts.train_general_classifier import (
    LABELS,
    ClassificationDataset,
    fixed_subset_loss,
    metrics,
    parse_args,
    stratified_sample,
)


def test_classifier_metrics_use_ground_truth_and_report_high_risk_recall() -> None:
    """每个真实类别都预测正确。

    检查准确率、综合分数、高风险召回和规范输出比例均为一。
    """
    rows = [{"output": label} for label in LABELS]
    report = metrics(rows, list(LABELS))
    assert report["accuracy"] == 1.0
    assert report["macroF1"] == 1.0
    assert report["highRiskRecall"] == 1.0
    assert report["outputValidity"] == 1.0


def test_classifier_metrics_count_invalid_generated_output() -> None:
    """把真实高风险样本预测成无效文字。

    检查它进入无效预测列，降低输出有效率并计为高风险遗漏。
    """
    rows = [{"output": label} for label in LABELS]
    report = metrics(rows, ["正常", "焦虑", "低落", "这不是一个标签"])
    assert report["outputValidity"] == 0.75
    assert report["confusionMatrix"]["高风险"]["__INVALID__"] == 1
    assert report["highRiskRecall"] == 0.0


def test_small_sample_is_seeded_and_label_stratified() -> None:
    """对相同数据使用同一种子抽取八个样本两次。

    检查结果一致且覆盖全部四类。
    """
    rows = [
        {"id": f"{label}-{index}", "output": label}
        for label in LABELS
        for index in range(5)
    ]
    first = stratified_sample(rows, 8, 42)
    second = stratified_sample(rows, 8, 42)
    assert first == second
    assert {row["output"] for row in first} == set(LABELS)


class _Tokenizer:
    eos_token = "!"

    def apply_chat_template(self, messages, **kwargs):
        return "prompt"

    def __call__(self, text, **kwargs):
        return {"input_ids": list(range(len(text)))}


def test_classifier_dataset_refuses_to_truncate_supervised_label():
    rows = [{"id": "length-boundary", "input": "test", "output": "焦虑"}]
    with pytest.raises(ValueError, match="length-boundary"):
        ClassificationDataset(rows, _Tokenizer(), max_length=6)


def test_classifier_dataset_preserves_all_target_tokens():
    dataset = ClassificationDataset([{"id": "target", "input": "test", "output": "焦虑"}], _Tokenizer(), max_length=9)
    assert dataset[0]["labels"] == [-100] * 6 + [6, 7, 8]


def test_training_only_cli_keeps_test_gate_separate(monkeypatch):
    monkeypatch.setattr("sys.argv", ["train", "--training-only", "--system-prompt-file", "contract.txt"])
    args = parse_args()
    assert args.training_only
    assert not args.sanity
    assert args.system_prompt_file == "contract.txt"


class _ProbeTokenizer(_Tokenizer):
    target_ids = {"正常": 1, "焦虑": 3, "低落": 5, "高风险": 7}
    pad_token_id = 0
    eos_token_id = 9

    def __call__(self, text, **kwargs):
        target = text.removeprefix("prompt").removesuffix(self.eos_token)
        ids = [0] * 6
        if target:
            ids += [self.target_ids[target], 9]
        if kwargs.get("return_tensors") == "pt":
            return {"input_ids": torch.tensor([ids]), "attention_mask": torch.ones(1, len(ids), dtype=torch.long)}
        return {"input_ids": ids}

    def decode(self, ids, **kwargs):
        return next(label for label, token_id in self.target_ids.items() if token_id == int(ids[0]))

    def save_pretrained(self, path):
        pass


@pytest.mark.parametrize(("counts", "expected_targets", "expected_loss"), [
    ([8, 8, 8, 8], [1, 1, 3, 3, 5, 5, 7, 7], 4.0),
    ([8, 0, 0, 8], [1, 1, 1, 1, 7, 7, 7, 7], 4.0),
    ([8, 1, 1, 1], [1, 1, 1, 1, 1, 3, 5, 7], 2.5),
])
def test_fixed_loss_probe_covers_labels_in_label_sorted_data(counts, expected_targets, expected_loss):
    rows = [
        {"id": f"{label}-{index}", "input": "probe", "output": label}
        for label, count in zip(LABELS, counts)
        for index in range(count)
    ]
    dataset = ClassificationDataset(rows, _ProbeTokenizer(), max_length=8)

    class ProbeModel:
        def __init__(self):
            self.targets = []

        def eval(self):
            return self

        def __call__(self, **batch):
            target = batch["labels"][batch["labels"] != -100][0].item()
            self.targets.append(target)
            return SimpleNamespace(loss=torch.tensor(float(target)))

    model = ProbeModel()
    first = fixed_subset_loss(model, dataset, "cpu", pad_token_id=0)
    first_targets = list(model.targets)
    second = fixed_subset_loss(model, dataset, "cpu", pad_token_id=0)
    # Expected losses use the literal target values, including absent and scarce labels.
    assert first == second == expected_loss
    assert model.targets[8:] == first_targets
    assert sorted(first_targets) == expected_targets


@pytest.mark.parametrize(("train_cases", "epochs", "expected_gradients"), [
    (5, 1, [0.25, 0.25]),
    (8, 1, [0.25, 0.25]),
    (5, 2, [0.25, 0.25, 0.25]),
])
def test_training_normalizes_full_and_partial_accumulation_windows(
    monkeypatch, tmp_path, train_cases, epochs, expected_gradients,
):
    import json

    class ToyModel(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.weight = torch.nn.Parameter(torch.tensor(0.0))
            self.config = SimpleNamespace(use_cache=True)
            self.training_losses = []

        def forward(self, input_ids, labels, attention_mask):
            loss = 1.0 + self.weight * 0.25
            if torch.is_grad_enabled():
                self.training_losses.append(loss.detach().item())
            return SimpleNamespace(loss=loss)

        def generate(self, input_ids, **kwargs):
            target_id = 1 if self.weight.item() < 0 else 3
            return torch.cat([input_ids, torch.tensor([[target_id, 9]])], dim=1)

        def save_pretrained(self, path):
            pass

    gradients = []

    class RecordingAdamW(torch.optim.AdamW):
        def step(self, *args, **kwargs):
            gradients.append(model.weight.grad.item())
            return super().step(*args, **kwargs)

    model = ToyModel()
    tokenizer = _ProbeTokenizer()
    train_path = tmp_path / "train.jsonl"
    validation_path = tmp_path / "validation.jsonl"
    result_path = tmp_path / "result.json"
    rows = [{"id": f"toy-{index}", "input": "probe", "output": "正常"} for index in range(train_cases)]
    train_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
    validation_path.write_text(json.dumps(rows[0], ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: False)
    monkeypatch.setattr(training.AutoTokenizer, "from_pretrained", lambda *args, **kwargs: tokenizer)
    monkeypatch.setattr(training.AutoModelForCausalLM, "from_pretrained", lambda *args, **kwargs: model)
    monkeypatch.setattr(training, "get_peft_model", lambda base, config: base)
    monkeypatch.setattr(training, "get_peft_model_state_dict", lambda base: base.state_dict())
    monkeypatch.setattr(training, "set_peft_model_state_dict", lambda base, state: base.load_state_dict(state))
    monkeypatch.setattr(torch.optim, "AdamW", RecordingAdamW)
    monkeypatch.setattr("sys.argv", [
        "train", "--model", "cpu-toy-only", "--train", str(train_path),
        "--validation", str(validation_path), "--test", str(tmp_path / "FORBIDDEN-TEST.jsonl"),
        "--output-dir", str(tmp_path / "output"), "--result", str(result_path),
        "--epochs", str(epochs), "--learning-rate", "0.01", "--batch-size", "1",
        "--gradient-accumulation", "4", "--max-length", "8", "--training-only", "--local-files-only",
    ])
    training.main()
    # Each microbatch contributes gradient 0.25; every window's mean must remain 0.25.
    # This observes real autograd at the optimizer, where gradients below 1 escape clipping.
    assert gradients == pytest.approx(expected_gradients)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["training"]["updates"] == len(expected_gradients)
    assert result["training"]["rawBatchLossMean"] == pytest.approx(
        sum(model.training_losses) / len(model.training_losses),
    )
    assert result["testDatasetRead"] is False
