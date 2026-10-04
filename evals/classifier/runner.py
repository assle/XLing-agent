"""Classifier evaluation runner for the fine-tuned emotion classifier.

Evaluates the classifier itself (4-class emotion labeling) on the held-out
validation set. Supports baseline (base model zero-shot) and fine-tuned model
runs via provider/model switches, so before/after comparison is apples-to-apples.

Run:
    python -m evals.classifier.runner
    python -m evals.classifier.runner --provider ollama --model qwen2.5:3b
    python -m evals.classifier.runner --provider openai --model gpt-4o-mini
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.core.versioning import ArtifactVersionResolver
from evals.config import EvalSettings, get_eval_settings

logger = logging.getLogger(__name__)

CLASSES = ["正常", "焦虑", "低落", "高风险"]

CLASSIFIER_SYSTEM = (
    "你是心理健康支持消息分类器。只输出一个标签词，不要解释、不要标点。"
    "可选标签：正常、焦虑、低落、高风险。"
    "正常：情绪平稳的日常表达；"
    "焦虑：紧张、担心、压力、未来导向的不安；"
    "低落：压抑、丧失兴趣、疲惫、情绪低沉；"
    "高风险：自伤、自杀、轻生意念或具体计划。"
)

HIGH_RISK_WORDS = ["自杀", "自残", "不想活", "结束生命", "伤害自己", "轻生", "suicide", "kill myself", "self harm"]
DEPRESSED_WORDS = ["抑郁", "低落", "崩溃", "难过", "丧失", "提不起", "行尸", "压抑", "无意义", "累赘", "想哭", "发呆", "机械", "低沉"]
CONSULT_WORDS = ["焦虑", "抑郁", "压力", "失眠", "难过", "崩溃", "痛苦", "无助", "心理", "咨询", "担心", "害怕", "逃避", "anxious", "depress", "stress"]


def load_dataset(path: Path) -> list[dict]:
    """逐行读取分类评估数据，跳过空行并解析每行对象。

    path 指向每行一个样本的文件；格式错误直接抛出，避免静默遗漏样本。
    """
    rows: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def normalize_label(raw: str) -> str:
    """与业务分类路径一致，仅去掉首尾空白，不从自由文本中猜测标签。"""
    return raw.strip()


def classify_mock(text: str) -> str:
    """用固定关键词优先级模拟分类器输出。

    高风险、低落和心理支持信号依次匹配，其余返回正常；只用于验证流程。
    """
    lowered = text.lower()
    if any(w in lowered for w in HIGH_RISK_WORDS):
        return "高风险"
    if any(w in lowered for w in DEPRESSED_WORDS):
        return "低落"
    if any(w in lowered for w in CONSULT_WORDS):
        return "焦虑"
    return "正常"


def classify_ollama(text: str, model: str, base_url: str) -> str:
    """向指定本地模型发送单条分类请求。

    text 为样本原文，model 和 base_url 明确选择待评估模型；请求失败直接抛出，外层记录失败样本。
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": CLASSIFIER_SYSTEM},
            {"role": "user", "content": text},
        ],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 16},
    }
    r = httpx.post(f"{base_url}/api/chat", json=payload, timeout=30)
    r.raise_for_status()
    return r.json()["message"]["content"].strip()


def classify_openai(text: str, model: str, base_url: str, api_key: str) -> str:
    """通过兼容远程聊天接口请求分类标签。

    使用传入地址、密钥和模型，限制输出长度；返回模型原文，标签提取另行处理。
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": CLASSIFIER_SYSTEM},
            {"role": "user", "content": text},
        ],
        "temperature": 0.1,
        "max_tokens": 16,
    }
    r = httpx.post(f"{base_url}/chat/completions", headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def compute_metrics(results: list[dict]) -> dict:
    """统计四类分类结果的准确率、各类命中表现和混淆矩阵。

    precision 表示预测为该类的结果有多少正确，recall 表示真实该类有多少被找回，F1 综合两者。
    未知预测不进入四类矩阵，但计入准确率分母和真实类别的漏判数。
    """
    classes = CLASSES
    total = max(1, len(results))

    # 矩阵行是真实类别，列是预测类别；对角线表示分类正确。
    confusion = {exp: {pred: 0 for pred in classes} for exp in classes}
    for r in results:
        exp = r["expected"]
        pred = r["predicted"]
        # 未知标签不写入四类矩阵；下面总体准确率仍会统计它为未命中样本。
        if exp in confusion and pred in confusion[exp]:
            confusion[exp][pred] += 1

    per_class: dict[str, dict[str, float]] = {}
    for cls in classes:
        tp = confusion[cls][cls]
        fp = sum(confusion[other][cls] for other in classes if other != cls)
        fn = sum(1 for row in results if row["expected"] == cls and row["predicted"] != cls)
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        per_class[cls] = {"precision": precision, "recall": recall, "f1": f1}

    # 每类等权平均，避免大量正常样本完全主导综合表现。
    macro_f1 = sum(per_class[cls]["f1"] for cls in classes) / len(classes)
    correct = sum(1 for r in results if r["expected"] == r["predicted"])
    accuracy = correct / total

    return {
        "totalCases": len(results),
        "accuracy": accuracy,
        "macroF1": macro_f1,
        "perClass": per_class,
        "confusionMatrix": confusion,
        "highRiskRecall": per_class["高风险"]["recall"],
    }


def evaluate(settings: EvalSettings | None = None, provider: str | None = None, model: str | None = None) -> dict:
    """遍历分类数据集调用所选模型，保存详细报告与不含样本的摘要。

    每条异常记为错误预测后继续；仅接受去掉首尾空白后严格属于四个标签的输出。
    输出平均每条耗时及版本信息，不自动训练或部署模型。
    """
    settings = settings or get_eval_settings()
    provider = provider or settings.cls_eval_ai_provider

    dataset_path = Path(settings.cls_eval_dataset)
    rows = load_dataset(dataset_path)
    logger.info("Loaded %d cases from %s", len(rows), dataset_path)

    resolved_model = model or settings.cls_eval_model or settings.ollama_classifier_model
    base_url = settings.cls_eval_base_url or settings.ollama_base_url
    api_key = settings.cls_eval_api_key or settings.openai_api_key

    results: list[dict] = []
    started = time.perf_counter()
    for row in rows:
        text = row["input"]
        expected = row["output"]
        raw = ""
        try:
            if provider == "mock":
                raw = classify_mock(text)
            elif provider == "openai":
                raw = classify_openai(text, resolved_model, base_url, api_key)
            else:
                raw = classify_ollama(text, resolved_model, base_url)
            predicted = normalize_label(raw)
        # 单条调用失败保留在评估结果中，不从总样本数量里删掉。
        except Exception as exc:
            logger.warning("classify failed for case: %s", type(exc).__name__)
            predicted = "__ERROR__"
        results.append({
            "input": text,
            "expected": expected,
            "predicted": predicted,
            "rawOutput": raw,
            "validOutput": raw.strip() in CLASSES,
            "hit": expected == predicted,
        })

    metrics = compute_metrics(results)
    elapsed = time.perf_counter() - started

    report = {
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "model": resolved_model,
        "dataset": str(dataset_path),
        "artifactVersion": ArtifactVersionResolver(
            settings.model_copy(update={"ai_provider": provider})
        ).current(dataset_path).to_dict(),
        **metrics,
        "outputValidity": sum(result["validOutput"] for result in results) / max(1, len(results)),
        "latencyMsPerCase": elapsed * 1000 / max(1, len(results)),
        "cases": results,
    }

    report_path = Path(settings.cls_eval_output)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = {k: v for k, v in report.items() if k != "cases"}
    summary_path = Path(settings.cls_eval_summary_output)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    return report


if __name__ == "__main__":
    provider = None
    model = None
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--provider" and i + 1 < len(args):
            provider = args[i + 1]
            i += 2
        elif args[i] == "--model" and i + 1 < len(args):
            model = args[i + 1]
            i += 2
        else:
            provider = args[i]
            i += 1

    report = evaluate(provider=provider, model=model)
    print(f"Classifier evaluation completed (provider={provider or 'default'}, model={report['model']}).")
    print(f"totalCases={report['totalCases']}")
    print(f"accuracy={report['accuracy']:.4f}")
    print(f"macroF1={report['macroF1']:.4f}")
    for cls in CLASSES:
        m = report["perClass"][cls]
        print(f"  {cls}: precision={m['precision']:.4f} recall={m['recall']:.4f} f1={m['f1']:.4f}")
    print(f"highRiskRecall={report['highRiskRecall']:.4f}")
    print(f"report={get_eval_settings().cls_eval_output}")
