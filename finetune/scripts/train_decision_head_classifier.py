"""训练单次前向决策头四分类器：冻结基座 + LoRA + 固定标签线性决策头。

借鉴 Strands Decider / Cloudflare Clef 的决策模型范式：不做自回归生成，
在分类提示的最后一个位置取隐藏表示，由决策头一次输出全部标签的 logits，
softmax 即得各类别概率。标签集合固定为本任务四类时，这是"选项打分"
架构的最简形态；标签在请求时定义的泛化能力不在本实验范围内。

输入包装、系统提示、LoRA 配置与 train_general_classifier.py 保持一致，
训练/验证划分与选模口径相同，结果可与已选生成式分类器直接对比。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
import time
from pathlib import Path
from typing import Literal

import torch
from peft import LoraConfig, get_peft_model, get_peft_model_state_dict, set_peft_model_state_dict
from torch import nn
from torch.utils.data import DataLoader, Dataset
from transformers import AutoModelForCausalLM, AutoTokenizer, get_cosine_schedule_with_warmup

ROOT = Path(__file__).resolve().parents[2]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from finetune.scripts.train_general_classifier import (  # noqa: E402
    LABELS,
    device_name,
    load_jsonl,
    metrics,
    stratified_sample,
)

DEFAULT_SYSTEM_PROMPT = "你是心理健康支持消息分类器。只输出一个标签：正常、焦虑、低落、高风险。分类只用于分流和安全信号，不作诊断。"


def parse_args() -> argparse.Namespace:
    """读取模型、数据文件、训练参数及运行模式；默认值对齐已选生成式训练。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="Qwen/Qwen2.5-0.5B-Instruct")
    parser.add_argument("--train", default="finetune/data/current-classifier/train-376.jsonl")
    parser.add_argument("--validation", default="finetune/data/current-classifier/validation-128.jsonl")
    parser.add_argument("--output-dir", default="finetune/saves/qwen25-05b-decision-head")
    parser.add_argument("--result", default="target/decision-head-training.json")
    parser.add_argument("--epochs", type=float, default=4.0)
    parser.add_argument("--learning-rate", type=float, default=5e-5)
    parser.add_argument("--head-learning-rate", type=float, default=1e-3, help="新建决策头的学习率，高于 LoRA")
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--gradient-accumulation", type=int, default=8)
    parser.add_argument("--max-length", type=int, default=256)
    parser.add_argument("--lora-rank", type=int, default=8)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-train-samples", type=int, default=0)
    parser.add_argument("--max-eval-samples", type=int, default=0)
    parser.add_argument("--local-files-only", action="store_true")
    parser.add_argument("--sanity", action="store_true")
    parser.add_argument("--training-only", action="store_true", help="保存选模适配器与决策头，不做额外测试")
    parser.add_argument("--system-prompt-file", default="", help="固定训练与评估使用的系统提示文件")
    parser.add_argument("--input-format", choices=("plain", "quoted"), default="quoted", help="与注册分类器一致的用户输入格式")
    return parser.parse_args()


def build_prompt(tokenizer, text: str, system_prompt: str, input_format: Literal["plain", "quoted"]) -> str:
    """用与线上一致的聊天模板组装分类提示，不含任何输出标签文本。"""
    from app.core.classifier_contract import classifier_user_content

    return tokenizer.apply_chat_template(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": classifier_user_content(text, input_format)},
        ],
        tokenize=False,
        add_generation_prompt=True,
    )


class DecisionDataset(Dataset):
    """把分类样本编码为提示 token 与类别索引；超长样本明确拒绝，不静默截断。"""

    def __init__(self, rows: list[dict], tokenizer, max_length: int, system_prompt: str, input_format: Literal["plain", "quoted"]):
        self.examples = []
        for row in rows:
            prompt = build_prompt(tokenizer, row["input"], system_prompt, input_format)
            input_ids = tokenizer(prompt, add_special_tokens=False)["input_ids"]
            if len(input_ids) > max_length:
                raise ValueError(f"分类样本 {row.get('id', 'unknown')} 提示超过 max_length={max_length}")
            self.examples.append({"input_ids": input_ids, "label": LABELS.index(row["output"]), "output": row["output"]})

    def __len__(self) -> int:
        return len(self.examples)

    def __getitem__(self, index: int) -> dict:
        return self.examples[index]


def collate(batch: list[dict], pad_token_id: int) -> dict[str, torch.Tensor]:
    """右侧补齐输入，注意力掩码区分真实位置；类别索引组成整型标签向量。"""
    width = max(len(item["input_ids"]) for item in batch)
    inputs, masks, labels = [], [], []
    for item in batch:
        padding = width - len(item["input_ids"])
        inputs.append(item["input_ids"] + [pad_token_id] * padding)
        masks.append([1] * len(item["input_ids"]) + [0] * padding)
        labels.append(item["label"])
    return {
        "input_ids": torch.tensor(inputs, dtype=torch.long),
        "attention_mask": torch.tensor(masks, dtype=torch.long),
        "labels": torch.tensor(labels, dtype=torch.long),
    }


class DecisionHeadClassifier(nn.Module):
    """在 PEFT 因果语言模型的骨干上挂固定标签线性头，单次前向输出各类别 logits。

    取每个样本最后一个真实 token 位置的隐藏表示（与生成式方案第一个输出
    token 的读取位置一致），决策头在 float32 下计算，避免半精度 softmax 误差。
    """

    def __init__(self, peft_model, hidden_size: int):
        super().__init__()
        self.peft_model = peft_model
        self.head = nn.Linear(hidden_size, len(LABELS), dtype=torch.float32)

    def forward(self, input_ids, attention_mask, labels=None):
        backbone = self.peft_model.model if hasattr(self.peft_model, "model") else self.peft_model
        # PEFT 包装下的基座 transformer 主体（Qwen2Model），不经过 LM 头。
        transformer = backbone.model if hasattr(backbone, "model") else backbone
        hidden = transformer(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state
        last_positions = attention_mask.sum(dim=1) - 1
        pooled = hidden[torch.arange(hidden.shape[0], device=hidden.device), last_positions]
        logits = self.head(pooled.to(torch.float32))
        loss = nn.functional.cross_entropy(logits, labels) if labels is not None else None
        return {"logits": logits, "loss": loss}

    def trainable_parameters(self) -> list:
        """LoRA 增量参数与决策头参数参与优化，基座其余权重保持冻结。"""
        return [parameter for parameter in self.parameters() if parameter.requires_grad]


def predict_probs(model: DecisionHeadClassifier, tokenizer, text: str, device: str, max_length: int,
                  system_prompt: str, input_format: Literal["plain", "quoted"]) -> list[float]:
    """对单条文本做一次前向，返回四类概率（顺序与 LABELS 一致）。"""
    prompt = build_prompt(tokenizer, text, system_prompt, input_format)
    encoded = tokenizer(
        prompt,
        add_special_tokens=False,
        truncation=True,
        max_length=max_length,
        return_tensors="pt",
    )
    input_ids = encoded["input_ids"].to(device)
    attention_mask = encoded["attention_mask"].to(device)
    with torch.no_grad():
        logits = model(input_ids=input_ids, attention_mask=attention_mask)["logits"]
        return torch.softmax(logits[0], dim=-1).cpu().tolist()


def evaluate(model, tokenizer, rows, device, max_length, system_prompt, input_format) -> dict:
    """逐条单次前向预测并统计指标、平均耗时与逐例概率明细。"""
    model.eval()
    predictions, cases = [], []
    started = time.perf_counter()
    for row in rows:
        probs = predict_probs(model, tokenizer, row["input"], device, max_length, system_prompt, input_format)
        best = max(range(len(LABELS)), key=lambda index: probs[index])
        predicted = LABELS[best]
        predictions.append(predicted)
        cases.append({
            "id": row["id"], "expected": row["output"], "predicted": predicted,
            "confidence": probs[best],
            "probabilities": {label: probs[index] for index, label in enumerate(LABELS)},
        })
    elapsed = time.perf_counter() - started
    return {**metrics(rows, predictions), "latencyMsPerCase": elapsed * 1000 / max(1, len(rows)), "casesDetail": cases}


def fixed_subset_loss(model, dataset, device, pad_token_id, limit: int = 8) -> float:
    """用固定分层的少量样本测量训练前后损失探针，仅作训练健全性检查。"""
    model.eval()
    values = []
    loader = DataLoader(
        stratified_sample(dataset.examples, limit, 42),
        batch_size=1,
        shuffle=False,
        collate_fn=lambda batch: collate(batch, pad_token_id),
    )
    with torch.no_grad():
        for index, batch in enumerate(loader):
            if index >= limit:
                break
            batch = {key: value.to(device) for key, value in batch.items()}
            values.append(float(model(**batch)["loss"].item()))
    if not values or not all(math.isfinite(value) for value in values):
        raise RuntimeError("固定样本损失不可用")
    return sum(values) / len(values)


def main() -> None:
    """训练 LoRA 与决策头，按验证 macro F1 选模，保存适配器与决策头权重。

    不读取任何固定回归测试集；损失探针只验证训练流程，不代表模型质量。
    """
    args = parse_args()
    system_prompt = DEFAULT_SYSTEM_PROMPT
    if args.system_prompt_file:
        system_prompt = (ROOT / args.system_prompt_file).read_text(encoding="utf-8").strip()
        if not system_prompt:
            raise ValueError("分类系统提示不能为空")
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    device = device_name()
    output_dir = ROOT / args.output_dir
    result_path = ROOT / args.result
    if args.sanity:
        args.max_train_samples = args.max_train_samples or 16
        args.max_eval_samples = args.max_eval_samples or 8
        args.epochs = 1.0
        output_dir = output_dir.with_name(output_dir.name + "-sanity")
        result_path = result_path.with_name(result_path.stem + "-sanity.json")
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path.parent.mkdir(parents=True, exist_ok=True)

    train_rows = load_jsonl(ROOT / args.train, args.max_train_samples)
    validation_rows = load_jsonl(ROOT / args.validation, args.max_eval_samples)
    tokenizer = AutoTokenizer.from_pretrained(
        args.model,
        trust_remote_code=True,
        cache_dir=ROOT / "data" / "huggingface",
        local_files_only=args.local_files_only,
    )
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    dtype = torch.float16 if device in {"mps", "cuda"} else torch.float32
    base_model = AutoModelForCausalLM.from_pretrained(
        args.model,
        trust_remote_code=True,
        cache_dir=ROOT / "data" / "huggingface",
        torch_dtype=dtype,
        low_cpu_mem_usage=True,
        local_files_only=args.local_files_only,
    ).to(device)
    base_model.config.use_cache = False
    peft_model = get_peft_model(base_model, LoraConfig(
        r=args.lora_rank,
        lora_alpha=args.lora_rank * 2,
        lora_dropout=0.05,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
        task_type="CAUSAL_LM",
    ))
    model = DecisionHeadClassifier(peft_model, base_model.config.hidden_size).to(device)
    dataset = DecisionDataset(train_rows, tokenizer, args.max_length, system_prompt, args.input_format)
    generator = torch.Generator().manual_seed(args.seed)
    loader = DataLoader(
        dataset,
        batch_size=args.batch_size,
        shuffle=True,
        generator=generator,
        collate_fn=lambda batch: collate(batch, tokenizer.pad_token_id),
    )
    lora_parameters = [parameter for name, parameter in model.named_parameters()
                       if parameter.requires_grad and "lora_" in name]
    optimizer = torch.optim.AdamW([
        {"params": lora_parameters, "lr": args.learning_rate},
        {"params": list(model.head.parameters()), "lr": args.head_learning_rate},
    ])
    update_steps = max(1, math.ceil(len(loader) * args.epochs / args.gradient_accumulation))
    scheduler = get_cosine_schedule_with_warmup(optimizer, max(1, update_steps // 10), update_steps)
    trainable = model.trainable_parameters()
    pre_train_loss = fixed_subset_loss(model, dataset, device, tokenizer.pad_token_id)
    losses = []
    validation_history = []
    best_validation_f1 = -1.0
    best_state = None
    best_head_state = None
    model.train()
    optimizer.zero_grad(set_to_none=True)
    total_batches = max(1, math.ceil(len(loader) * args.epochs))
    batch_index = 0
    while batch_index < total_batches:
        for batch in loader:
            if batch_index >= total_batches:
                break
            if batch_index % args.gradient_accumulation == 0:
                accumulation_batches = min(args.gradient_accumulation, total_batches - batch_index)
            batch_index += 1
            batch = {key: value.to(device) for key, value in batch.items()}
            loss = model(**batch)["loss"] / accumulation_batches
            if not torch.isfinite(loss):
                raise RuntimeError(f"训练损失不是有限数：batch={batch_index}")
            loss.backward()
            losses.append(float(loss.item() * accumulation_batches))
            if batch_index % args.gradient_accumulation == 0 or batch_index == total_batches:
                torch.nn.utils.clip_grad_norm_(trainable, 1.0)
                optimizer.step()
                scheduler.step()
                optimizer.zero_grad(set_to_none=True)
            if batch_index % 20 == 0 or batch_index == total_batches:
                print(f"TRAIN batch={batch_index}/{total_batches} loss={sum(losses[-20:]) / len(losses[-20:]):.4f}", flush=True)
            if batch_index % len(loader) == 0 or batch_index == total_batches:
                validation = evaluate(model, tokenizer, validation_rows, device, args.max_length, system_prompt, args.input_format)
                validation_history.append({"batch": batch_index, **{key: validation[key] for key in ("accuracy", "macroF1", "highRiskRecall", "outputValidity", "latencyMsPerCase")}})
                print(f"VALID batch={batch_index} macroF1={validation['macroF1']:.4f} highRiskRecall={validation['highRiskRecall']:.4f}", flush=True)
                if validation["macroF1"] > best_validation_f1:
                    best_validation_f1 = validation["macroF1"]
                    best_state = {key: value.detach().cpu().clone()
                                  for key, value in get_peft_model_state_dict(model.peft_model).items()}
                    best_head_state = {key: value.detach().cpu().clone() for key, value in model.head.state_dict().items()}
                model.train()

    if best_state is None or best_head_state is None:
        raise RuntimeError("验证阶段没有选出可用检查点")
    set_peft_model_state_dict(model.peft_model, best_state)
    model.head.load_state_dict(best_head_state)
    post_train_loss = fixed_subset_loss(model, dataset, device, tokenizer.pad_token_id)
    model.peft_model.save_pretrained(output_dir / "adapter")
    tokenizer.save_pretrained(output_dir / "adapter")
    torch.save({
        "head_state_dict": best_head_state,
        "labels": list(LABELS),
        "hiddenSize": base_model.config.hidden_size,
        "systemPromptSHA256": hashlib.sha256(system_prompt.encode()).hexdigest(),
        "inputFormat": args.input_format,
    }, output_dir / "head.pt")
    validation = evaluate(model, tokenizer, validation_rows, device, args.max_length, system_prompt, args.input_format)
    training = {
        "initialLoss": pre_train_loss,
        "finalLoss": post_train_loss,
        "updates": update_steps,
        "rawBatchLossMean": sum(losses) / len(losses),
        "validationHistory": validation_history,
        "bestValidationMacroF1": best_validation_f1,
    }
    result = {
        "experimental": True,
        "sanity": args.sanity,
        "trainingOnly": True,
        "architecture": "frozen-base + LoRA + fixed-label linear decision head; single forward pass, no generation",
        "model": args.model,
        "device": device,
        "seed": args.seed,
        "config": vars(args),
        "systemPromptSHA256": hashlib.sha256(system_prompt.encode()).hexdigest(),
        "trainCases": len(train_rows),
        "validationCases": len(validation_rows),
        "testCases": 0,
        "testDatasetRead": False,
        "training": training,
        "validation": validation,
        "passed": math.isfinite(post_train_loss) and post_train_loss < pre_train_loss,
    }
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"device": device, "training": training, "passed": result["passed"]}, ensure_ascii=False, indent=2))
    if not result["passed"]:
        raise RuntimeError("训练检查未通过：训练损失没有下降")


if __name__ == "__main__":
    main()
