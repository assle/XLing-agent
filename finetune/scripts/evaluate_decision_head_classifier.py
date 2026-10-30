"""在固定回归集上评测决策头分类器，输出逐例概率与延迟，供与已选模型对比。

只评测，不训练、不修改任何运行时配置；结果文件必须为新文件。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer

ROOT = Path(__file__).resolve().parents[2]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from finetune.scripts.evaluate_runtime_classifier import LABEL_MAP  # noqa: E402
from finetune.scripts.train_decision_head_classifier import DecisionHeadClassifier, predict_probs  # noqa: E402
from finetune.scripts.train_general_classifier import LABELS, device_name, load_jsonl, metrics  # noqa: E402


def parse_args() -> argparse.Namespace:
    """读取基座、适配器、决策头与数据集参数；可重复传入多个数据集。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--head", required=True, help="训练保存的 head.pt 路径")
    parser.add_argument("--dataset", action="append", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--system-prompt-file", required=True)
    parser.add_argument("--input-format", choices=("plain", "quoted"), default="quoted")
    parser.add_argument("--max-length", type=int, default=256)
    parser.add_argument("--local-files-only", action="store_true")
    return parser.parse_args()


def percentile(values: list[float], ratio: float) -> float:
    """计算最近秩百分位；空序列返回 0。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(ratio * len(ordered) + 0.999999) - 1))
    return ordered[index]


def main() -> None:
    """加载一次模型，逐数据集评测并汇总，写出完整逐例结果与分位延迟。"""
    args = parse_args()
    output_path = ROOT / args.output
    if output_path.exists():
        raise FileExistsError(f"结果文件已存在，拒绝覆盖：{output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    system_prompt = (ROOT / args.system_prompt_file).read_text(encoding="utf-8").strip()
    head_payload = torch.load(ROOT / args.head, map_location="cpu", weights_only=False)
    if head_payload["labels"] != list(LABELS):
        raise ValueError("决策头标签与当前四类定义不一致")
    device = device_name()
    tokenizer = AutoTokenizer.from_pretrained(
        args.model, trust_remote_code=True, cache_dir=ROOT / "data" / "huggingface",
        local_files_only=args.local_files_only,
    )
    dtype = torch.float16 if device in {"mps", "cuda"} else torch.float32
    base_model = AutoModelForCausalLM.from_pretrained(
        args.model, trust_remote_code=True, cache_dir=ROOT / "data" / "huggingface",
        torch_dtype=dtype, low_cpu_mem_usage=True, local_files_only=args.local_files_only,
    ).to(device)
    base_model.config.use_cache = False
    peft_model = PeftModel.from_pretrained(base_model, ROOT / args.adapter)
    model = DecisionHeadClassifier(peft_model, head_payload["hiddenSize"]).to(device)
    model.head.load_state_dict(head_payload["head_state_dict"])
    model.eval()

    datasets = []
    all_rows, all_predictions, all_latencies = [], [], []
    for dataset_arg in args.dataset:
        rows = load_jsonl(ROOT / dataset_arg)
        # 与运行时评测入口一致：自然数据集的英文枚举标签映射回四类中文标签。
        for row in rows:
            row["output"] = LABEL_MAP.get(row["output"], row["output"])
        predictions, cases, latencies = [], [], []
        for row in rows:
            started = time.perf_counter()
            probs = predict_probs(model, tokenizer, row["input"], device, args.max_length, system_prompt, args.input_format)
            latencies.append((time.perf_counter() - started) * 1000)
            best = max(range(len(LABELS)), key=lambda index: probs[index])
            predictions.append(LABELS[best])
            cases.append({
                "id": row["id"], "expected": row["output"], "predicted": LABELS[best],
                "confidence": probs[best], "latencyMs": latencies[-1],
                "probabilities": {label: probs[index] for index, label in enumerate(LABELS)},
            })
        summary = {
            "path": dataset_arg,
            **metrics(rows, predictions),
            "latencyP50Ms": percentile(latencies, 0.50),
            "latencyP95Ms": percentile(latencies, 0.95),
            "casesDetail": cases,
        }
        datasets.append(summary)
        all_rows.extend(rows)
        all_predictions.extend(predictions)
        all_latencies.extend(latencies)
        print(f"EVAL {dataset_arg} accuracy={summary['accuracy']:.4f} macroF1={summary['macroF1']:.4f} "
              f"highRiskRecall={summary['highRiskRecall']:.4f} p50={summary['latencyP50Ms']:.1f}ms", flush=True)

    result = {
        "experimental": True,
        "architecture": "frozen-base + LoRA + fixed-label linear decision head; single forward pass",
        "model": args.model,
        "adapter": args.adapter,
        "head": args.head,
        "device": device,
        "systemPromptFile": args.system_prompt_file,
        "inputFormat": args.input_format,
        "combined": {
            **metrics(all_rows, all_predictions),
            "latencyP50Ms": percentile(all_latencies, 0.50),
            "latencyP95Ms": percentile(all_latencies, 0.95),
        },
        "datasets": datasets,
        "scope": "Model-only evaluation on the fixed exposed synthetic regression sets; no keyword policy, risk trajectory or user-facing graph.",
    }
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"combined": {key: result["combined"][key] for key in
                                   ("cases", "accuracy", "macroF1", "highRiskRecall", "latencyP50Ms", "latencyP95Ms")}},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
