"""对比微调前后的分类器评估报告，产出 before/after 提升数字。

前置：已用 cls_eval runner 跑过两轮：
  - baseline: CLS_EVAL_OUTPUT=target/cls-eval-baseline.json python -m evals.classifier.runner --provider ollama --model qwen2.5:3b
  - after:    python -m evals.classifier.runner --provider ollama --model xling-cls-3b-ft:latest

用法：
    python finetune/scripts/compare_eval.py
    python finetune/scripts/compare_eval.py --before path/baseline.json --after path/after.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BEFORE = ROOT / "target" / "cls-eval-baseline.json"
DEFAULT_AFTER = ROOT / "target" / "cls-eval-report.json"
DEFAULT_OUTPUT = ROOT / "target" / "cls-eval-comparison.json"

CLASSES = ["正常", "焦虑", "低落", "高风险"]


def load_report(path: Path) -> dict:
    """读取已有分类评估报告。

    文件缺失时打印路径并以失败状态结束脚本；格式错误由文本解析直接抛出。
    """
    if not path.exists():
        print(f"报告不存在: {path}")
        sys.exit(1)
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def compare(before: dict, after: dict) -> dict:
    """比较微调前后的准确率、分类综合分数和高风险召回率。

    差值统一按 after 减 before；高风险门槛允许下降不超过 0.05，不等于完全不下降。
    此处不校验两份报告的数据集和版本是否一致，调用方须保证比较条件。
    """
    def _summary(r: dict) -> dict:
        """从一份报告提取模型、提供方及三个总体指标。

        模型和提供方缺失时使用空文本，必要指标缺失则抛错。
        """
        return {
            "model": r.get("model", ""),
            "provider": r.get("provider", ""),
            "accuracy": r["accuracy"],
            "macroF1": r["macroF1"],
            "highRiskRecall": r["highRiskRecall"],
        }

    return {
        "before": _summary(before),
        "after": _summary(after),
        "delta": {
            "accuracy": after["accuracy"] - before["accuracy"],
            "macroF1": after["macroF1"] - before["macroF1"],
            "highRiskRecall": after["highRiskRecall"] - before["highRiskRecall"],
        },
        "beforePerClass": before["perClass"],
        "afterPerClass": after["perClass"],
        "highRiskRecallGate": after["highRiskRecall"] >= before["highRiskRecall"] - 0.05,
    }


def main() -> None:
    """解析前后报告和输出路径，保存比较结果并打印分类别变化。

    使用已有报告计算，不重新调用分类模型，也不根据门槛自动替换线上模型。
    """
    before_path = DEFAULT_BEFORE
    after_path = DEFAULT_AFTER
    output_path = DEFAULT_OUTPUT

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--before" and i + 1 < len(args):
            before_path = Path(args[i + 1])
            i += 2
        elif args[i] == "--after" and i + 1 < len(args):
            after_path = Path(args[i + 1])
            i += 2
        elif args[i] == "--output" and i + 1 < len(args):
            output_path = Path(args[i + 1])
            i += 2
        else:
            i += 1

    before = load_report(before_path)
    after = load_report(after_path)
    result = compare(before, after)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=== 微调前后对比 ===")
    print(f"before: model={result['before']['model']}  accuracy={result['before']['accuracy']:.4f}  macroF1={result['before']['macroF1']:.4f}  highRiskRecall={result['before']['highRiskRecall']:.4f}")
    print(f"after:  model={result['after']['model']}  accuracy={result['after']['accuracy']:.4f}  macroF1={result['after']['macroF1']:.4f}  highRiskRecall={result['after']['highRiskRecall']:.4f}")
    print(f"delta:  accuracy={result['delta']['accuracy']:+.4f}  macroF1={result['delta']['macroF1']:+.4f}  highRiskRecall={result['delta']['highRiskRecall']:+.4f}")
    print()
    for cls in CLASSES:
        b = result["beforePerClass"][cls]
        a = result["afterPerClass"][cls]
        print(f"  {cls}: F1 {b['f1']:.4f} -> {a['f1']:.4f} ({a['f1'] - b['f1']:+.4f})  recall {b['recall']:.4f} -> {a['recall']:.4f}")
    print()
    gate = result["highRiskRecallGate"]
    print(f"高风险 recall 准入门槛: {'通过' if gate else '未通过（after 显著低于 before，建议扩数据或调阈值）'}")
    print(f"对比报告: {output_path}")


if __name__ == "__main__":
    main()
