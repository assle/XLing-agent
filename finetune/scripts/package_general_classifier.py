"""把通过门槛的 LoRA 适配器合并成可供 Ollama 导入的模型目录。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer

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
    return parser.parse_args()


def main() -> None:
    """检查评估门槛通过后，将微调增量参数合并进基础模型并保存。

    同时保存分词器和说明清单，标记已打包但未激活、人工审核尚未完成。
    会加载模型并写出模型文件，不执行本地服务注册或线上切换。
    """
    args = parse_args()
    results = json.loads((ROOT / args.results).read_text(encoding="utf-8"))
    if not results.get("replacementGate", {}).get("passed"):
        raise RuntimeError("替换门槛未通过，不生成部署模型")
    output = ROOT / args.output
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
    manifest = {
        "baseModel": args.model,
        "adapter": args.adapter,
        "sourceResults": args.results,
        "replacementGatePassed": True,
        "humanReviewComplete": False,
        "deploymentStatus": "packaged-not-activated",
    }
    (output / "xling-package-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
