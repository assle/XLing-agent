"""在本地 MPS/CUDA/CPU 上微调通用心理支持四分类生成模型。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Literal

import torch
from peft import LoraConfig, get_peft_model, get_peft_model_state_dict, set_peft_model_state_dict
from torch.utils.data import DataLoader, Dataset
from transformers import AutoModelForCausalLM, AutoTokenizer, get_cosine_schedule_with_warmup

ROOT = Path(__file__).resolve().parents[2]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))
LABELS = ("正常", "焦虑", "低落", "高风险")
SYSTEM_PROMPT = "你是心理健康支持消息分类器。只输出一个标签：正常、焦虑、低落、高风险。分类只用于分流和安全信号，不作诊断。"
INPUT_FORMAT: Literal["plain", "quoted"] = "plain"


def parse_args() -> argparse.Namespace:
    """读取模型、数据文件、训练参数及运行模式。

    返回参数对象；只声明选项和默认值，不在此加载模型或验证文件。
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="Qwen/Qwen2.5-0.5B-Instruct")
    parser.add_argument("--train", default="finetune/data/general-train.jsonl")
    parser.add_argument("--validation", default="finetune/data/general-val.jsonl")
    parser.add_argument("--test", default="finetune/data/general-test.jsonl")
    parser.add_argument("--output-dir", default="finetune/saves/qwen25-05b-general-cls")
    parser.add_argument("--result", default="target/general-classifier-results.json")
    parser.add_argument("--epochs", type=float, default=2.0)
    parser.add_argument("--learning-rate", type=float, default=2e-4)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--gradient-accumulation", type=int, default=8)
    parser.add_argument("--max-length", type=int, default=128)
    parser.add_argument("--lora-rank", type=int, default=8)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-train-samples", type=int, default=0)
    parser.add_argument("--max-eval-samples", type=int, default=0)
    parser.add_argument("--max-latency-ms", type=float, default=2000.0)
    parser.add_argument("--local-files-only", action="store_true")
    parser.add_argument("--sanity", action="store_true")
    parser.add_argument("--training-only", action="store_true", help="保存验证集选出的适配器，不读取测试集")
    parser.add_argument("--system-prompt-file", default="", help="固定训练与评估使用的系统提示文件")
    parser.add_argument("--input-format", choices=("plain", "quoted"), default="plain", help="与注册分类器一致的用户输入格式")
    return parser.parse_args()


def load_jsonl(path: str | Path, limit: int = 0) -> list[dict]:
    """读取逐行样本文件，可按类别限制样本数量。

    limit 为零时返回全部；启用抽样时当前固定使用种子 42，与训练入口的随机种子参数分开。
    """
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    return stratified_sample(rows, limit, 42) if limit else rows


def stratified_sample(rows: list[dict], limit: int, seed: int) -> list[dict]:
    """按四类轮流取样，尽量保持小样本集合的类别覆盖。

    每类先用指定种子打乱；上限至少要容纳四个标签，类别耗尽后继续从其他类别取样。
    """
    if not limit or limit >= len(rows):
        return list(rows)
    if limit < len(LABELS):
        raise ValueError(f"样本上限至少为 {len(LABELS)}，才能覆盖全部标签")
    rng = random.Random(seed)
    by_label = {label: [row for row in rows if row["output"] == label] for label in LABELS}
    for pool in by_label.values():
        rng.shuffle(pool)
    selected: list[dict] = []
    while len(selected) < limit:
        progressed = False
        for label in LABELS:
            if by_label[label] and len(selected) < limit:
                selected.append(by_label[label].pop())
                progressed = True
        if not progressed:
            break
    return selected


def prompt_text(tokenizer, text: str, input_format: Literal["plain", "quoted"] | None = None) -> str:
    """使用模型分词器提供的聊天模板组装分类请求。

    返回尚未编码的文本，并添加模型开始回答所需的提示标记。
    """
    from app.core.classifier_contract import classifier_user_content

    return tokenizer.apply_chat_template(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": classifier_user_content(text, input_format or INPUT_FORMAT)},
        ],
        tokenize=False,
        add_generation_prompt=True,
    )


class ClassificationDataset(Dataset):
    def __init__(self, rows: list[dict], tokenizer, max_length: int):
        """把分类样本编码成模型输入和用于训练的目标编号。

        提示词部分的目标设为 -100，使损失只关注输出标签及结束标记。
        超过 max_length 时拒绝样本，避免静默截掉标签、产生没有监督目标的训练。
        """
        self.examples = []
        for row in rows:
            prompt = prompt_text(tokenizer, row["input"])
            prompt_ids = tokenizer(prompt, add_special_tokens=False)["input_ids"]
            full_ids = tokenizer(
                prompt + row["output"] + tokenizer.eos_token,
                add_special_tokens=False,
            )["input_ids"]
            if len(full_ids) > max_length or len(full_ids) <= len(prompt_ids):
                raise ValueError(f"分类样本 {row.get('id', 'unknown')} 没有完整监督目标，请调整 max_length")
            # 让模型只学习该输出标签；提示词本身不计入本次监督训练的损失。
            labels = [-100] * min(len(prompt_ids), len(full_ids)) + full_ids[len(prompt_ids):]
            self.examples.append({"input_ids": full_ids, "labels": labels, "output": row["output"]})

    def __len__(self) -> int:
        """返回预先编码好的样本数量。

        供数据加载器计算批次数，不重新读取文件。
        """
        return len(self.examples)

    def __getitem__(self, index: int) -> dict:
        """按索引返回已编码样本字典。

        返回保存对象本身，没有额外复制；越界按列表规则抛错。
        """
        return self.examples[index]


def collate(batch: list[dict], pad_token_id: int) -> dict[str, torch.Tensor]:
    """把一个批次补到相同长度，并转换为模型使用的整数张量。

    输入补 pad_token_id，训练目标补 -100，注意力掩码将真实文本与补齐位置区分。
    返回输入、训练目标和掩码三组张量，要求批次非空。
    """
    width = max(len(item["input_ids"]) for item in batch)
    inputs, labels, masks = [], [], []
    for item in batch:
        padding = width - len(item["input_ids"])
        inputs.append(item["input_ids"] + [pad_token_id] * padding)
        labels.append(item["labels"] + [-100] * padding)
        masks.append([1] * len(item["input_ids"]) + [0] * padding)
    return {
        "input_ids": torch.tensor(inputs, dtype=torch.long),
        "labels": torch.tensor(labels, dtype=torch.long),
        "attention_mask": torch.tensor(masks, dtype=torch.long),
    }


def device_name() -> str:
    """按可用性选择 NVIDIA 显卡、苹果图形加速或中央处理器。

    依次检查 cuda、mps，均不可用时用 cpu；只选择设备名称，不搬移模型。
    """
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def generate_label(
    model, tokenizer, text: str, device: str, max_length: int, metadata: dict | None = None,
) -> tuple[str, str]:
    """关闭梯度计算，以确定性生成取得一个中文分类标签。

    输入按最大长度截断，输出只解码新增部分；严格属于四类才接受，否则标为无效并保留原文。
    """
    prompt = prompt_text(tokenizer, text)
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
        generated = model.generate(
            input_ids=input_ids,
            attention_mask=attention_mask,
            do_sample=False,
            temperature=None,
            top_p=None,
            top_k=None,
            max_new_tokens=6,
            pad_token_id=tokenizer.pad_token_id,
            eos_token_id=tokenizer.eos_token_id,
        )
    raw = tokenizer.decode(generated[0, input_ids.shape[1]:], skip_special_tokens=True).strip()
    if metadata is not None:
        metadata.update(
            generatedTokens=int(generated.shape[1] - input_ids.shape[1]),
            promptTokens=int(input_ids.shape[1]),
            finishReason="eos" if int(generated[0, -1]) == tokenizer.eos_token_id else "length",
        )
    return (raw if raw in LABELS else "__INVALID__"), raw


def metrics(rows: list[dict], predictions: list[str]) -> dict:
    """统计分类结果和无效输出，计算总体与分类别表现。

    混淆矩阵单独保留无效预测列，使其计入真实类别漏报；调用方应保证样本和预测数量相同。
    """
    prediction_labels = (*LABELS, "__INVALID__")
    confusion = {expected: {predicted: 0 for predicted in prediction_labels} for expected in LABELS}
    for row, predicted in zip(rows, predictions):
        bucket = predicted if predicted in LABELS else "__INVALID__"
        confusion[row["output"]][bucket] += 1
    per_class = {}
    for label in LABELS:
        true_positive = confusion[label][label]
        false_positive = sum(confusion[other][label] for other in LABELS if other != label)
        false_negative = sum(confusion[label][other] for other in prediction_labels if other != label)
        precision = true_positive / max(1, true_positive + false_positive)
        recall = true_positive / max(1, true_positive + false_negative)
        f1 = 2 * precision * recall / max(1e-12, precision + recall)
        per_class[label] = {"precision": precision, "recall": recall, "f1": f1}
    accuracy = sum(row["output"] == prediction for row, prediction in zip(rows, predictions)) / max(1, len(rows))
    return {
        "cases": len(rows),
        "accuracy": accuracy,
        "macroF1": sum(item["f1"] for item in per_class.values()) / len(LABELS),
        "highRiskRecall": per_class["高风险"]["recall"],
        "perClass": per_class,
        "confusionMatrix": confusion,
        "predictionDistribution": dict(Counter(predictions)),
        "outputValidity": sum(prediction in LABELS for prediction in predictions) / max(1, len(predictions)),
    }


def fixed_subset_loss(model, dataset: ClassificationDataset, device: str, pad_token_id: int, limit: int = 8) -> float:
    """用固定分层的少量样本测量损失探针。

    默认至多八条，尽量均衡覆盖数据中存在的标签，不代表全数据损失。
    切换为评估模式且不计算梯度，便于比较训练前后；无结果或损失非有限数时抛错。
    """
    model.eval()
    values = []
    loader = DataLoader(
        stratified_sample(dataset.examples, limit, 42),
        batch_size=1,
        shuffle=False,
        # 匿名整理函数把加载器提供的样本列表补齐，使用本次固定的补齐编号。
        collate_fn=lambda batch: collate(batch, pad_token_id),
    )
    with torch.no_grad():
        for index, batch in enumerate(loader):
            if index >= limit:
                break
            batch = {key: value.to(device) for key, value in batch.items()}
            values.append(float(model(**batch).loss.item()))
    if not values or not all(math.isfinite(value) for value in values):
        raise RuntimeError("固定样本损失不可用")
    return sum(values) / len(values)


def evaluate(model, tokenizer, rows: list[dict], device: str, max_length: int) -> dict:
    """逐条生成分类结果并统计效果及平均每例耗时。

    保留样本编号、真实标签、规范预测和模型原文，供后续定位错误。
    """
    model.eval()
    predictions, cases = [], []
    started = time.perf_counter()
    for row in rows:
        predicted, raw = generate_label(model, tokenizer, row["input"], device, max_length)
        predictions.append(predicted)
        cases.append({
            "id": row["id"], "expected": row["output"], "predicted": predicted,
            "rawOutput": raw if raw in LABELS else "__INVALID__", "outputCharacters": len(raw),
        })
    elapsed = time.perf_counter() - started
    return {**metrics(rows, predictions), "latencyMsPerCase": elapsed * 1000 / max(1, len(rows)), "casesDetail": cases}


def main() -> None:
    """执行微调训练、按验证集选择最佳增量参数，再评估独立测试集。

    LoRA 是仅训练少量附加参数的微调方式；梯度累积将多个小批次的梯度合并后更新，减少设备内存需求。
    健全性和 training-only 模式检查训练损失下降，不读取测试集；正式模式在参数选定后比较基础模型与微调模型。
    保存增量参数、训练记录和替换门槛结果，不自动激活模型。
    """
    global SYSTEM_PROMPT, INPUT_FORMAT
    args = parse_args()
    INPUT_FORMAT = args.input_format
    if args.system_prompt_file:
        SYSTEM_PROMPT = (ROOT / args.system_prompt_file).read_text(encoding="utf-8").strip()
        if not SYSTEM_PROMPT:
            raise ValueError("分类系统提示不能为空")
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    device = device_name()
    output_dir = ROOT / args.output_dir
    result_path = ROOT / args.result
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path.parent.mkdir(parents=True, exist_ok=True)

    if args.sanity:
        args.max_train_samples = args.max_train_samples or 16
        args.max_eval_samples = args.max_eval_samples or 8
        args.epochs = 1.0
        if args.result == "target/general-classifier-results.json":
            args.result = "target/general-classifier-sanity.json"
        if args.output_dir == "finetune/saves/qwen25-05b-general-cls":
            args.output_dir = "finetune/saves/qwen25-05b-general-cls-sanity"
        output_dir = ROOT / args.output_dir
        result_path = ROOT / args.result
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

    model = get_peft_model(base_model, LoraConfig(
        r=args.lora_rank,
        lora_alpha=args.lora_rank * 2,
        lora_dropout=0.05,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
        task_type="CAUSAL_LM",
    ))
    dataset = ClassificationDataset(train_rows, tokenizer, args.max_length)
    generator = torch.Generator().manual_seed(args.seed)
    loader = DataLoader(
        dataset,
        batch_size=args.batch_size,
        shuffle=True,
        generator=generator,
        # 每批使用当前分词器的补齐编号，确保输入长度一致且补齐位置不参与损失。
        collate_fn=lambda batch: collate(batch, tokenizer.pad_token_id),
    )
    # 优化器只更新允许训练的增量参数，基础模型其余权重保持冻结。
    parameters = [parameter for parameter in model.parameters() if parameter.requires_grad]
    pre_train_loss = fixed_subset_loss(model, dataset, device, tokenizer.pad_token_id)
    optimizer = torch.optim.AdamW(parameters, lr=args.learning_rate)
    update_steps = max(1, math.ceil(len(loader) * args.epochs / args.gradient_accumulation))
    scheduler = get_cosine_schedule_with_warmup(optimizer, max(1, update_steps // 10), update_steps)
    losses = []
    validation_history = []
    best_validation_f1 = -1.0
    best_state = None
    model.train()
    optimizer.zero_grad(set_to_none=True)
    total_batches = max(1, math.ceil(len(loader) * args.epochs))
    batch_index = 0
    while batch_index < total_batches:
        for batch in loader:
            if batch_index >= total_batches:
                break
            if batch_index % args.gradient_accumulation == 0:
                # 同一累积组使用固定分母，末组只按实际剩余批数平均。
                accumulation_batches = min(args.gradient_accumulation, total_batches - batch_index)
            batch_index += 1
            batch = {key: value.to(device) for key, value in batch.items()}
            # 先缩小每批损失再累积梯度，使多个小批次共同完成一次参数更新。
            loss = model(**batch).loss / accumulation_batches
            if not torch.isfinite(loss):
                raise RuntimeError(f"训练损失不是有限数：batch={batch_index}")
            loss.backward()
            losses.append(float(loss.item() * accumulation_batches))
            if batch_index % args.gradient_accumulation == 0 or batch_index == total_batches:
                # 参数更新前限制梯度整体大小，降低某一批次梯度过大带来的不稳定。
                torch.nn.utils.clip_grad_norm_(parameters, 1.0)
                optimizer.step()
                scheduler.step()
                optimizer.zero_grad(set_to_none=True)
            if batch_index % 20 == 0 or batch_index == total_batches:
                print(f"TRAIN batch={batch_index}/{total_batches} loss={sum(losses[-20:]) / len(losses[-20:]):.4f}", flush=True)

            if batch_index % len(loader) == 0 or batch_index == total_batches:
                validation = evaluate(model, tokenizer, validation_rows, device, args.max_length)
                validation_history.append({"batch": batch_index, **{key: validation[key] for key in ("accuracy", "macroF1", "highRiskRecall", "outputValidity", "latencyMsPerCase")}})
                # 仅根据验证集表现选择参数，复制到中央处理器内存，避免后续训练继续改写最佳版本。
                if validation["macroF1"] > best_validation_f1:
                    best_validation_f1 = validation["macroF1"]
                    best_state = {
                        key: value.detach().cpu().clone()
                        for key, value in get_peft_model_state_dict(model).items()
                    }
                model.train()

    if best_state is None:
        raise RuntimeError("验证阶段没有选出可用检查点")
    set_peft_model_state_dict(model, best_state)
    post_train_loss = fixed_subset_loss(model, dataset, device, tokenizer.pad_token_id)
    model.save_pretrained(output_dir / "adapter")
    tokenizer.save_pretrained(output_dir / "adapter")
    validation = evaluate(model, tokenizer, validation_rows, device, args.max_length)
    initial_loss = pre_train_loss
    final_loss = post_train_loss
    training = {
        "initialLoss": initial_loss,
        "finalLoss": final_loss,
        "updates": update_steps,
        "rawBatchLossMean": sum(losses) / len(losses),
        "validationHistory": validation_history,
        "bestValidationMacroF1": best_validation_f1,
    }
    # 两种隔离训练模式均不打开测试集，损失下降不代表模型质量已通过。
    if args.sanity or args.training_only:
        result = {
            "experimental": True,
            "sanity": args.sanity,
            "trainingOnly": args.training_only,
            "model": args.model,
            "device": device,
            "seed": args.seed,
            "config": vars(args),
            "systemPromptSHA256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
            "trainCases": len(train_rows),
            "validationCases": len(validation_rows),
            "testCases": 0,
            "testDatasetRead": False,
            "training": training,
            "validation": validation,
            "passed": math.isfinite(final_loss) and final_loss < initial_loss,
        }
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"device": device, "training": training, "passed": result["passed"]}, ensure_ascii=False, indent=2))
        if not result["passed"]:
            raise RuntimeError("训练检查未通过：训练损失没有下降")
        return

    # 最终测试集在最佳参数已确定后才读取，避免用测试成绩反复挑选训练版本。
    test_rows = load_jsonl(ROOT / args.test, args.max_eval_samples)
    # 暂时关闭附加参数得到同一基础模型的对照结果，随后恢复附加参数在相同样本上比较。
    with model.disable_adapter():
        baseline = evaluate(model, tokenizer, test_rows, device, args.max_length)
    final = evaluate(model, tokenizer, test_rows, device, args.max_length)
    gate = {
        "highRiskRecallNotWorse": final["highRiskRecall"] >= baseline["highRiskRecall"],
        "macroF1Improved": final["macroF1"] > baseline["macroF1"],
        "accuracyNotWorse": final["accuracy"] >= baseline["accuracy"],
        "outputValidityNotWorse": final["outputValidity"] >= baseline["outputValidity"],
        "latencyWithinBudget": final["latencyMsPerCase"] <= args.max_latency_ms,
    }
    gate["passed"] = all(gate.values())
    result = {
        "experimental": True,
        "humanReviewComplete": False,
        "model": args.model,
        "adapter": str(output_dir / "adapter"),
        "device": device,
        "seed": args.seed,
        "config": vars(args),
        "trainCases": len(train_rows),
        "validationCases": len(validation_rows),
        "testCases": len(test_rows),
        "testDatasetReadAfterSelection": True,
        "training": training,
        "baseline": baseline,
        "validation": validation,
        "final": final,
        "replacementGate": gate,
        "decision": "qualified-for-packaging" if gate["passed"] else "keep-current-model",
    }
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("device", "training", "replacementGate", "decision")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
