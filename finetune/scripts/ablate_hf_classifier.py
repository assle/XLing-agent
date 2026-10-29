"""Paired ablation of the selected LoRA on its actual historical base, without training."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import sys
import time
from collections import Counter
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from finetune.scripts import train_general_classifier as training  # noqa: E402
from finetune.scripts.evaluate_runtime_classifier import (  # noqa: E402
    CLASSES,
    LABEL_MAP,
    load_dataset,
    runtime_metrics,
    sha256,
    write_report,
)

ARMS = ("withoutSelectedLora", "withSelectedLora")


def paired_summary(pairs: list[dict]) -> dict:
    if not pairs or any(set(pair["arms"]) != set(ARMS) or
                        any(arm.get("status") != "completed" for arm in pair["arms"].values()) for pair in pairs):
        raise ValueError("Complete paired observations are required")
    rows = {arm: [{**pair["arms"][arm], "expected": pair["expected"]} for pair in pairs] for arm in ARMS}
    metrics = {arm: runtime_metrics(values) for arm, values in rows.items()}
    improved, regressed, both_correct, both_wrong = [], [], 0, 0
    for pair in pairs:
        before = pair["arms"][ARMS[0]]["predicted"] == pair["expected"]
        after = pair["arms"][ARMS[1]]["predicted"] == pair["expected"]
        if after and not before:
            improved.append(pair["key"])
        elif before and not after:
            regressed.append(pair["key"])
        elif before:
            both_correct += 1
        else:
            both_wrong += 1
    common_valid = [pair for pair in pairs if all(pair["arms"][arm]["strictValid"] for arm in ARMS)]
    common_accuracy = {
        arm: sum(pair["arms"][arm]["predicted"] == pair["expected"] for pair in common_valid) / len(common_valid)
        if common_valid else None for arm in ARMS
    }
    return {
        "metrics": metrics, "improved": improved, "regressed": regressed,
        "bothCorrect": both_correct, "bothWrong": both_wrong,
        "accuracyDeltaPercentagePoints": 100 * (len(improved) - len(regressed)) / len(pairs),
        "macroF1Delta": metrics[ARMS[1]]["macroF1"] - metrics[ARMS[0]]["macroF1"],
        "highRiskRecallDelta": metrics[ARMS[1]]["highRiskRecall"] - metrics[ARMS[0]]["highRiskRecall"],
        "falseHighCountDelta": metrics[ARMS[1]]["falseHighCount"] - metrics[ARMS[0]]["falseHighCount"],
        "commonValidCases": len(common_valid), "commonValidAccuracy": common_accuracy,
    }


def new_pair(row: dict) -> dict:
    key = row["datasetSHA256"] + ":" + row["id"]
    order = list(ARMS)
    if hashlib.sha256(key.encode()).digest()[0] % 2:
        order.reverse()
    return {**row, "key": key, "order": order, "arms": {}}


def observe_pair(model, pair: dict, generate) -> None:
    for arm in pair["order"]:
        context = model.disable_adapter() if arm == ARMS[0] else nullcontext()
        with context:
            status = model.get_model_status()
            if status.merged_adapters or status.enabled is not (arm == ARMS[1]):
                raise ValueError("Unmerged adapter enabled/disabled state must match the arm")
            pair["arms"][arm] = {"status": "started", "adapterEnabled": status.enabled}
            pair["arms"][arm] = {"status": "completed", "adapterEnabled": status.enabled, **generate(pair["input"])}
        if model.get_model_status().enabled is not True:
            raise ValueError("Adapter state was not restored")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Ablation output must be a new file")
    frozen_path = ROOT / "finetune/reports/current-classifier/training/freeze.json"
    selected_path = ROOT / "finetune/reports/classifier-selection.json"
    system_path = ROOT / "finetune/reports/current-classifier/training/system-prompt.txt"
    frozen = json.loads(frozen_path.read_text())
    selected = json.loads(selected_path.read_text())["runtimeSelection"]
    model_files = {p.name: sha256(p) for p in sorted(args.model.iterdir()) if p.is_file()}
    adapter_files = {p.name: sha256(p) for p in sorted(args.adapter.iterdir()) if p.is_file()}
    if model_files.get("model.safetensors") != frozen["baseModelWeightsSHA256"]:
        raise ValueError("Base does not match the actual training base")
    if adapter_files.get("adapter_model.safetensors") != selected["selectedAdapterSHA256"]:
        raise ValueError("Adapter does not match the selected 376-row candidate")
    adapter_config = json.loads((args.adapter / "adapter_config.json").read_text())
    if adapter_config["peft_type"] != "LORA" or adapter_config["bias"] != "none" or adapter_config["modules_to_save"]:
        raise ValueError("This experiment isolates the selected low-rank adapter only")
    training.SYSTEM_PROMPT = system_path.read_text().strip()
    training.INPUT_FORMAT = "quoted"
    effective_system_sha = hashlib.sha256(training.SYSTEM_PROMPT.encode()).hexdigest()
    if effective_system_sha != frozen["systemPromptSHA256"]:
        raise ValueError("Effective system prompt changed")
    rows, datasets = [], []
    for path in args.dataset:
        dataset_sha = sha256(path)
        data = load_dataset(path)
        if not data or len({row["id"] for row in data}) != len(data):
            raise ValueError("Each fixed dataset must contain unique IDs")
        datasets.append({"path": str(path.resolve()), "sha256": dataset_sha, "rows": len(data)})
        for row in data:
            expected = LABEL_MAP.get(row["output"], row["output"])
            if expected not in CLASSES:
                raise ValueError("Unsupported frozen target")
            rows.append({"id": row["id"], "sourceGroup": row.get("sourceGroup"), "input": row["input"],
                         "expected": expected, "datasetSHA256": dataset_sha,
                         "inputSHA256": hashlib.sha256(row["input"].encode()).hexdigest()})
    if len({row["sha256"] for row in datasets}) != len(datasets):
        raise ValueError("Datasets must not be repeated")
    from peft import PeftModel

    device = training.device_name()
    dtype = training.torch.float32 if device == "cpu" else training.torch.float16
    training.torch.manual_seed(42)
    tokenizer = training.AutoTokenizer.from_pretrained(args.model, local_files_only=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    for row in rows:
        prompt = training.prompt_text(tokenizer, row["input"])
        row["promptSHA256"] = hashlib.sha256(prompt.encode()).hexdigest()
        row["untruncatedPromptTokens"] = len(tokenizer(prompt, add_special_tokens=False)["input_ids"])
        if row["untruncatedPromptTokens"] > 256:
            raise ValueError("Fixed input exceeds the matched 256-token contract")
    base = training.AutoModelForCausalLM.from_pretrained(args.model, local_files_only=True, torch_dtype=dtype,
                                                       low_cpu_mem_usage=True)
    model = PeftModel.from_pretrained(base, args.adapter, is_trainable=False).to(device)
    model.eval()
    model.config.use_cache = False
    model.generation_config.use_cache = False
    model.generation_config.repetition_penalty = 1.0
    if model.get_model_status().trainable_params != 0:
        raise ValueError("Inference-only experiment must have no trainable parameters")
    report = {
        "status": "running", "createdAtUTC": datetime.now(timezone.utc).isoformat(),
        "scope": "Incremental effect of the selected376 LoRA on its actual historical merged base",
        "cleanPristineQwenBase": False, "optimizerUpdates": 0, "attempts": 0,
        "limitations": ["Historical base already contains earlier merged fine-tuning",
                        "Fixed synthetic engineering corpus; 120 old cases exposed,40 previously tested",
                        "Single HF run; no population/clinical generalization or new independent test claim",
                        "Latency is descriptive, not a statistically controlled serving benchmark"],
        "device": device, "baseDtype": str(dtype), "model": str(args.model.resolve()),
        "adapter": str(args.adapter.resolve()), "modelFilesSHA256": model_files,
        "adapterFilesSHA256": adapter_files, "adapterConfig": adapter_config,
        "adapterParameterDtypes": dict(Counter(str(p.dtype) for name, p in model.named_parameters() if "lora_" in name)),
        "packages": {name: importlib.metadata.version(name) for name in ("torch", "transformers", "peft", "safetensors")},
        "sourceSHA256": {str(p.relative_to(ROOT)): sha256(p) for p in (
            Path(__file__), ROOT / "finetune/scripts/train_general_classifier.py",
            ROOT / "app/core/classifier_contract.py", ROOT / "finetune/scripts/evaluate_runtime_classifier.py")},
        "frozenInputReferences": {str(p.relative_to(ROOT)): sha256(p) for p in (frozen_path, selected_path, system_path)},
        "systemSHA256": effective_system_sha, "inputFormat": "quoted", "datasets": datasets,
        "controls": {"oneLoadedModel": True, "onlyFactor": "Selected adapter enabled versus disabled",
                     "seed": 42, "doSample": False, "maxNewTokens": 6, "maxInputTokens": 256,
                     "useCache": False, "repetitionPenalty": 1.0, "customProcessor": None,
                     "strictOutput": "Exactly one Chinese label and EOS; invalid outputs remain in denominator"},
        "pairs": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_report(args.output, report)

    def generate(text: str) -> dict:
        report["attempts"] += 1
        started = time.perf_counter()
        metadata = {}
        predicted, raw = training.generate_label(model, tokenizer, text, device, 256, metadata=metadata)
        if device == "mps":
            training.torch.mps.synchronize()
        valid = predicted in CLASSES and metadata["finishReason"] == "eos"
        return {"predicted": predicted if valid else "__INVALID__", "raw": raw, "strictValid": valid,
                "invalidReason": None if valid else ("length" if metadata["finishReason"] == "length" else "non_label_output"),
                "latencyMs": (time.perf_counter() - started) * 1000, **metadata}

    try:
        for row in rows:
            pair = new_pair(row)
            report["pairs"].append(pair)
            write_report(args.output, report)
            observe_pair(model, pair, generate)
            write_report(args.output, report)
            if len(report["pairs"]) % 20 == 0:
                print(json.dumps({"completedPairs": len(report["pairs"]), "attempts": report["attempts"]}), flush=True)
        report["summary"] = paired_summary(report["pairs"])
        report["datasetSummaries"] = [{**data, **paired_summary([
            pair for pair in report["pairs"] if pair["datasetSHA256"] == data["sha256"]])} for data in datasets]
        if any(sha256(args.model / name) != digest for name, digest in model_files.items()):
            raise ValueError("Base files changed during the experiment")
        if any(sha256(args.adapter / name) != digest for name, digest in adapter_files.items()):
            raise ValueError("Adapter files changed during the experiment")
        report.update(status="completed", completedAtUTC=datetime.now(timezone.utc).isoformat(), weightsUnchanged=True)
    except BaseException as exc:
        report.update(status="failed", errorType=type(exc).__name__)
        raise
    finally:
        write_report(args.output, report)
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
