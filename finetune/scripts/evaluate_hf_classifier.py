"""Evaluate one locked local adapter on fixed datasets without retraining."""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, action="append", required=True)
    parser.add_argument("--system-prompt-file", type=Path, required=True)
    parser.add_argument("--input-format", choices=("plain", "quoted"), default="quoted")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError("Fixed adapter observation already exists")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    training.SYSTEM_PROMPT = args.system_prompt_file.read_text(encoding="utf-8").strip()
    training.INPUT_FORMAT = args.input_format
    device = training.device_name()
    tokenizer = training.AutoTokenizer.from_pretrained(args.model, local_files_only=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    from peft import PeftModel

    base = training.AutoModelForCausalLM.from_pretrained(
        args.model, local_files_only=True, torch_dtype=training.torch.float16 if device != "cpu" else training.torch.float32,
        low_cpu_mem_usage=True,
    )
    model = PeftModel.from_pretrained(base, args.adapter).to(device)
    model.config.use_cache = False
    model.generation_config.repetition_penalty = 1.0
    model.eval()
    datasets = [{"path": str(path.resolve()), "sha256": sha256(path)} for path in args.dataset]
    report = {
        "status": "running", "provider": "hf", "device": device,
        "model": str(args.model.resolve()), "adapter": str(args.adapter.resolve()),
        "adapterSHA256": sha256(args.adapter / "adapter_model.safetensors"),
        "systemSHA256": sha256(args.system_prompt_file), "inputFormat": args.input_format,
        "datasets": datasets, "optimizerUpdates": 0, "cases": [],
    }
    write_report(args.output, report)
    for path, dataset in zip(args.dataset, datasets):
        rows = load_dataset(path)
        if not rows or len({row["id"] for row in rows}) != len(rows):
            raise ValueError("Expected nonempty fixed dataset with unique IDs")
        dataset["rows"] = len(rows)
        for row in rows:
            expected = LABEL_MAP.get(row["output"], row["output"])
            if expected not in CLASSES:
                raise ValueError("Unsupported target label")
            started = time.perf_counter()
            metadata = {}
            predicted, raw = training.generate_label(model, tokenizer, row["input"], device, 256, metadata=metadata)
            valid = predicted in CLASSES and metadata["finishReason"] == "eos"
            report["cases"].append({
                "id": row["id"], "sourceGroup": row.get("sourceGroup"),
                "datasetSHA256": dataset["sha256"], "expected": expected,
                "predicted": predicted if valid else "__INVALID__",
                "inputSHA256": hashlib.sha256(row["input"].encode()).hexdigest(),
                "strictValid": valid,
                "invalidReason": None if valid else ("length" if metadata["finishReason"] == "length" else "non_label_output"),
                "outputCharacters": len(raw), "latencyMs": (time.perf_counter() - started) * 1000,
                **metadata,
            })
            write_report(args.output, report)
        print({"completed": len(report["cases"]), "datasetRows": len(rows)}, flush=True)
    report["metrics"] = runtime_metrics(report["cases"])
    report["datasetMetrics"] = [
        {**dataset, **runtime_metrics([case for case in report["cases"] if case["datasetSHA256"] == dataset["sha256"]])}
        for dataset in datasets
    ]
    report["status"] = "completed"
    write_report(args.output, report)
    print(report["metrics"], flush=True)


if __name__ == "__main__":
    main()
