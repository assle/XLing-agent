"""用训练保存的验证集概率拟合决策头分类器的温度校准常数。

复用 app.services.risk_calibration.TemperatureScaler 的网格搜索；
softmax(log(p)/T) 与对原始 logits 做温度缩放等价（log p 与 logits 仅差
每行一个归一化常数），因此无需重新前向即可在已保存概率上拟合。
只读取 training-only 阶段产生的验证集预测，不触碰任何测试集。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from app.services.risk_calibration import TemperatureScaler  # noqa: E402
from finetune.scripts.train_general_classifier import LABELS  # noqa: E402


def parse_args() -> argparse.Namespace:
    """读取训练结果文件与校准输出路径；输出文件必须为新文件。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--training-result", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> None:
    """拟合温度并写出校准文件，记录拟合前后的验证集 NLL 供审计。"""
    args = parse_args()
    training_path = ROOT / args.training_result
    output_path = ROOT / args.output
    if output_path.exists():
        raise FileExistsError(f"校准文件已存在，拒绝覆盖：{output_path}")
    result = json.loads(training_path.read_text(encoding="utf-8"))
    if result.get("testDatasetRead") is not False:
        raise ValueError("训练结果不符合 training-only 口径")
    cases = result["validation"]["casesDetail"]
    logits, labels = [], []
    for case in cases:
        probabilities = case["probabilities"]
        logits.append([math.log(max(probabilities[label], 1e-12)) for label in LABELS])
        labels.append(LABELS.index(case["expected"]))
    scaler = TemperatureScaler.fit(logits, labels)
    payload = {
        "temperature": scaler.temperature,
        "labels": list(LABELS),
        "validationCases": len(cases),
        "nllBefore": TemperatureScaler(1.0).negative_log_likelihood(logits, labels),
        "nllAfter": scaler.negative_log_likelihood(logits, labels),
        "sourceTrainingResult": args.training_result,
        "sourceTrainingResultSHA256": hashlib.sha256(training_path.read_bytes()).hexdigest(),
        "note": "Fitted on the training-only validation split (128 rows); probabilities from the selected checkpoint. Thresholds remain engineering defaults pending fresh blind evaluation.",
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("temperature", "validationCases", "nllBefore", "nllAfter")}, indent=2))


if __name__ == "__main__":
    main()
