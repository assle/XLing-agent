"""合并通过替换门槛或显式仅供影子验证的 LoRA 适配器。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def parse_args() -> argparse.Namespace:
    """读取基础模型、适配参数、评估结果和合并输出位置。

    local-files-only 可限制模型读取为已有本地缓存，未指定项使用脚本默认值。
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="Qwen/Qwen2.5-0.5B-Instruct")
    parser.add_argument("--adapter", default="finetune/saves/qwen25-05b-general-cls/adapter")
    parser.add_argument("--results", default="target/general-classifier-results.json")
    parser.add_argument("--output", default="finetune/saves/qwen25-05b-general-cls-merged")
    parser.add_argument("--local-files-only", action="store_true")
    parser.add_argument("--shadow", action="store_true", help="仅生成隔离影子候选，不声称替换门槛通过")
    return parser.parse_args()


def packaging_manifest(args: argparse.Namespace, results: dict) -> dict:
    """Preserve the observed gate result while distinguishing a shadow-only package."""
    passed = results.get("replacementGate", {}).get("passed") is True
    if not passed and not args.shadow:
        raise RuntimeError("替换门槛未通过，不生成部署模型")
    manifest = {
        "baseModel": args.model,
        "adapter": args.adapter,
        "sourceResults": args.results,
        "replacementGatePassed": passed,
        "deploymentStatus": "shadow-only" if args.shadow else "packaged-not-activated",
    }
    if args.shadow:
        manifest["shadowOnly"] = True
    else:
        manifest["humanReviewComplete"] = False
    return manifest


def main() -> None:
    """将微调增量合并进基座；常规打包检查替换门槛，影子模式使用新隔离目录。

    同时保存分词器和实际门槛结果，区分未激活包与仅供影子验证的候选。
    会加载模型并写出模型文件，不执行本地服务注册或线上切换。
    """
    args = parse_args()
    results = json.loads((ROOT / args.results).read_text(encoding="utf-8"))
    manifest = packaging_manifest(args, results)
    output = ROOT / args.output
    if args.shadow and (output.exists() or output == ROOT / "finetune/saves/qwen25-05b-general-cls-merged"):
        raise RuntimeError("影子候选必须使用新的隔离输出目录")
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        args.model,
        trust_remote_code=True,
        cache_dir=ROOT / "data" / "huggingface",
        local_files_only=args.local_files_only,
    )
    base = AutoModelForCausalLM.from_pretrained(
        args.model,
        trust_remote_code=True,
        cache_dir=ROOT / "data" / "huggingface",
        local_files_only=args.local_files_only,
        torch_dtype=torch.float16,
        low_cpu_mem_usage=True,
    )
    merged = PeftModel.from_pretrained(base, ROOT / args.adapter).merge_and_unload()
    output.mkdir(parents=True, exist_ok=True)
    merged.save_pretrained(output, safe_serialization=True)
    tokenizer.save_pretrained(output)
    (output / "xling-package-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
