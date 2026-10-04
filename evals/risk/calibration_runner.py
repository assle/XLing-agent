from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from app.core.enums import RiskLevel
from app.core.versioning import ArtifactVersionResolver
from app.services.risk_calibration import (
    RISK_LEVELS,
    CalibratedRiskEngine,
    ClassConditionalConformalPredictor,
    TemperatureScaler,
)
from evals.config import EvalSettings, get_eval_settings


def collect_calibration_inputs(
    settings: EvalSettings | None = None,
) -> tuple[list[dict], list[dict]]:
    """按切分清单提取互不重叠的校准样本和最终测试样本。

    验证编号没有交集且都存在于源数据，再生成规则代理分数并写入两个输出文件。
    """
    settings = settings or get_eval_settings()
    source_rows = json.loads(
        Path(settings.risk_calibration_source_dataset).read_text(encoding="utf-8")
    )
    split = json.loads(Path(settings.risk_calibration_split).read_text(encoding="utf-8"))
    calibration_ids = set(split["calibration"])
    test_ids = set(split["test"])
    # 校准拟合用的样本不能同时用于最终测试，先检查编号交集。
    if calibration_ids.intersection(test_ids):
        raise ValueError("校准集与最终测试集不得重叠")
    rows_by_id = {row["id"]: row for row in source_rows}
    missing = calibration_ids.union(test_ids) - rows_by_id.keys()
    if missing:
        raise ValueError(f"切分清单包含未知案例：{sorted(missing)}")
    def collect(ids: set[str]) -> list[dict]:
        """按排序后的编号从外层源数据索引构造一组实验输入。

        保留原文和真实风险，分数由离线规则生成，确保重复运行输入顺序一致。
        """
        return [
            {
                "id": row_id,
                "text": rows_by_id[row_id]["text"],
                "expectedRisk": rows_by_id[row_id]["expected_risk"],
                "logits": _offline_logits(rows_by_id[row_id]["text"]),
            }
            for row_id in sorted(ids)
        ]

    calibration = collect(calibration_ids)
    test = collect(test_ids)
    for rows, output in (
        (calibration, Path(settings.risk_calibration_dataset)),
        (test, Path(settings.risk_calibration_test_dataset)),
    ):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return calibration, test


def _offline_logits(text: str) -> list[float]:
    """根据关键词返回固定的三类原始分数代理值。

    不读取线上模型概率，也不调用远程服务；这些分数只用于离线校准流程实验。
    """
    normalized = text.lower()
    if any(term in normalized for term in ("自杀", "自残", "不想活", "轻生")):
        return [-4.0, 0.0, 5.0]
    if any(term in normalized for term in ("低落", "抑郁", "无意义", "难过")):
        return [0.0, 4.0, -2.0]
    if any(term in normalized for term in ("焦虑", "压力", "担心", "失眠")):
        return [3.0, 1.0, -4.0]
    return [4.0, 0.0, -4.0]


def evaluate_calibration(settings: EvalSettings | None = None) -> dict:
    """在校准集拟合温度和分类阈值，再在最终测试集比较概率与候选集合表现。

    同时输出固定置信度、原始概率和校准概率指标，并记录数据切分和探索性限制。
    根据代码内门槛生成建议及参数文件，不实际部署；scoreSource 是配置推导标签，本函数直接读取文件分数，不能证明其真实来源。
    """
    settings = settings or get_eval_settings()
    calibration_path = Path(settings.risk_calibration_dataset)
    test_path = Path(settings.risk_calibration_test_dataset)
    calibration_rows = json.loads(calibration_path.read_text(encoding="utf-8"))
    test_rows = json.loads(test_path.read_text(encoding="utf-8"))

    calibration_logits = [row["logits"] for row in calibration_rows]
    calibration_labels = [_label_index(row["expectedRisk"]) for row in calibration_rows]
    scaler = TemperatureScaler.fit(calibration_logits, calibration_labels)
    calibration_probabilities = [scaler.probabilities(row) for row in calibration_logits]
    conformal = ClassConditionalConformalPredictor.fit(
        calibration_probabilities,
        calibration_labels,
        alpha=settings.risk_conformal_alpha,
    )
    engine = CalibratedRiskEngine(
        scaler,
        conformal,
        model_version=settings.ollama_classifier_model,
    )

    # 参数只在校准数据上拟合；从这里开始用独立测试数据检查表现。
    test_logits = [row["logits"] for row in test_rows]
    test_labels = [_label_index(row["expectedRisk"]) for row in test_rows]
    raw_probabilities = [TemperatureScaler(1.0).probabilities(row) for row in test_logits]
    fixed_confidence_probabilities = [
        _fixed_confidence_probabilities(row) for row in test_logits
    ]
    calibrated_probabilities = [scaler.probabilities(row) for row in test_logits]
    decisions = [engine.predict(row) for row in test_logits]
    fixed_confidence_metrics = _probability_metrics(
        fixed_confidence_probabilities,
        test_labels,
    )
    raw_metrics = _probability_metrics(raw_probabilities, test_labels)
    calibrated_metrics = _probability_metrics(calibrated_probabilities, test_labels)
    conformal_metrics = _conformal_metrics(decisions, test_labels)
    # 除了计算指标，还检查每类校准样本数量和来源组证据，避免过度解释小样本结果。
    calibration_counts = Counter(row["expectedRisk"] for row in calibration_rows)
    data_splits = _data_split_evidence(settings, calibration_rows, test_rows)
    score_source = (
        "mock-label-adapter"
        if settings.risk_eval_ai_provider == "mock"
        else "configured-classifier-logits-endpoint"
    )
    exploratory_reasons = []
    if min(calibration_counts.values(), default=0) < settings.risk_calibration_min_cases_per_class:
        exploratory_reasons.append("每类校准样本少于预设最低数量")
    if not data_splits["sourceGroupsVerified"]:
        exploratory_reasons.append("缺少可验证的同源改写分组")
    if score_source == "mock-label-adapter":
        exploratory_reasons.append("当前结果使用 Mock 规则分数，不是生产分类器原始 logits")
    exploratory = bool(exploratory_reasons)
    ece_improvement = (
        (raw_metrics["ece"] - calibrated_metrics["ece"]) / raw_metrics["ece"]
        if raw_metrics["ece"] > 0
        else 0.0
    )
    target_coverage = 1.0 - settings.risk_conformal_alpha
    # 报告建议要求同时满足数据依据、召回、校准改进及覆盖条件；此处不修改线上设置。
    deployable = not exploratory and (
        calibrated_metrics["highRiskRecall"] >= raw_metrics["highRiskRecall"]
        and ece_improvement >= 0.20
        and abs(conformal_metrics["coverage"] - target_coverage) <= 0.03
    )

    artifact = engine.to_dict()
    artifact_path = Path(settings.risk_calibration_artifact_output)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")

    report = {
        "artifactVersion": ArtifactVersionResolver(
            settings.model_copy(update={"ai_provider": settings.risk_eval_ai_provider})
        ).current(calibration_path).to_dict(),
        "calibrationVersion": engine.calibration_version,
        "calibrationCases": len(calibration_rows),
        "testCases": len(test_rows),
        "scoreSource": score_source,
        "dataSplits": data_splits,
        "calibrationCasesByClass": dict(calibration_counts),
        "exploratory": exploratory,
        "exploratoryReasons": exploratory_reasons,
        "coverageGuarantee": deployable,
        "fixedConfidence": fixed_confidence_metrics,
        "raw": raw_metrics,
        "calibrated": calibrated_metrics,
        "conformal": conformal_metrics,
        "eceRelativeImprovement": ece_improvement,
        "targetCoverage": target_coverage,
        "deployable": deployable,
        "decision": "enable-calibrated-risk" if deployable else "keep-current-risk-path",
        "cases": [
            {
                "id": row["id"],
                "expectedRisk": row["expectedRisk"],
                "rawProbabilities": decision.raw_probabilities,
                "calibratedProbabilities": decision.probabilities,
                "predictionSet": [risk.value for risk in decision.prediction_set],
                "uncertain": decision.uncertain,
                "requiresReview": decision.requires_review,
            }
            for row, decision in zip(test_rows, decisions, strict=True)
        ],
    }
    output_path = Path(settings.risk_calibration_output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _probability_metrics(probabilities: list[list[float]], labels: list[int]) -> dict:
    """根据最高概率类别统计准确率、分类表现及概率校准误差。

    高风险召回单独计算，避免总体准确率掩盖高风险遗漏；空分母通过至少一的保护避免除零。
    """
    predicted = [max(range(len(row)), key=row.__getitem__) for row in probabilities]
    accuracy = sum(pred == label for pred, label in zip(predicted, labels, strict=True)) / max(1, len(labels))
    high_index = RISK_LEVELS.index(RiskLevel.HIGH)
    high_total = sum(label == high_index for label in labels)
    high_hits = sum(
        pred == high_index and label == high_index
        for pred, label in zip(predicted, labels, strict=True)
    )
    return {
        "accuracy": accuracy,
        "macroF1": _macro_f1(predicted, labels),
        "highRiskRecall": high_hits / max(1, high_total),
        "ece": _expected_calibration_error(probabilities, labels),
        "brier": _brier_score(probabilities, labels),
    }


def _fixed_confidence_probabilities(
    logits: list[float],
    confidence: float = 0.8,
) -> list[float]:
    """为原始分数最高的类别分配固定置信度，其余均分剩余概率。

    这是用于比较的规则基准，不是从分类器取得的真实概率。
    """
    predicted = max(range(len(logits)), key=logits.__getitem__)
    remainder = (1.0 - confidence) / max(1, len(logits) - 1)
    return [confidence if index == predicted else remainder for index in range(len(logits))]


def _macro_f1(predicted: list[int], labels: list[int]) -> float:
    """分别计算各风险类别的 F1，再对类别等权平均。

    逐类统计正确命中、误报和漏报，类别样本较少也保留相同权重。
    """
    scores = []
    for label in range(len(RISK_LEVELS)):
        true_positive = sum(
            prediction == label and expected == label
            for prediction, expected in zip(predicted, labels, strict=True)
        )
        false_positive = sum(
            prediction == label and expected != label
            for prediction, expected in zip(predicted, labels, strict=True)
        )
        false_negative = sum(
            prediction != label and expected == label
            for prediction, expected in zip(predicted, labels, strict=True)
        )
        denominator = 2 * true_positive + false_positive + false_negative
        scores.append(2 * true_positive / denominator if denominator else 0.0)
    return sum(scores) / len(scores)


def _expected_calibration_error(
    probabilities: list[list[float]],
    labels: list[int],
    bins: int = 10,
) -> float:
    """按置信度分组，计算每组平均置信度与实际正确率的差距。

    用组内样本占比加权求和；区间左开右闭，第一组额外接纳零置信度。
    """
    total = max(1, len(labels))
    error = 0.0
    for bin_index in range(bins):
        lower = bin_index / bins
        upper = (bin_index + 1) / bins
        members = []
        for row, label in zip(probabilities, labels, strict=True):
            predicted = max(range(len(row)), key=row.__getitem__)
            confidence = row[predicted]
            # 边界采用左开右闭，让正好落在分箱边缘的样本只进入一个区间。
            if lower < confidence <= upper or (bin_index == 0 and confidence == 0):
                members.append((confidence, predicted == label))
        if members:
            mean_confidence = sum(item[0] for item in members) / len(members)
            accuracy = sum(item[1] for item in members) / len(members)
            error += len(members) / total * abs(accuracy - mean_confidence)
    return error


def _brier_score(probabilities: list[list[float]], labels: list[int]) -> float:
    """计算预测概率与真实类别指示值之间的平均平方误差。

    每个样本先把三类误差相加，再对样本平均，数值越小表示概率更贴近真实标签。
    """
    scores = []
    for row, label in zip(probabilities, labels, strict=True):
        scores.append(sum((probability - (index == label)) ** 2 for index, probability in enumerate(row)))
    return sum(scores) / max(1, len(scores))


def _conformal_metrics(decisions, labels: list[int]) -> dict:
    """统计风险候选集合的覆盖率、平均大小和审核比例。

    覆盖表示真实类别在集合内，不表示最终选择必然正确；审核比例取预测结果的审核标志。
    """
    covered = 0
    set_size = 0
    reviews = 0
    for decision, label in zip(decisions, labels, strict=True):
        expected = RISK_LEVELS[label]
        covered += expected in decision.prediction_set
        set_size += len(decision.prediction_set)
        reviews += decision.requires_review
    total = max(1, len(labels))
    return {
        "coverage": covered / total,
        "averageSetSize": set_size / total,
        "reviewRate": reviews / total,
    }


def _label_index(value: str) -> int:
    """将风险文本转换为固定类别顺序中的编号。

    非法风险名称会抛错，避免被静默映射到低风险。
    """
    return RISK_LEVELS.index(RiskLevel(value))


def _data_split_evidence(
    settings: EvalSettings,
    calibration_rows: list[dict],
    test_rows: list[dict],
) -> dict:
    """汇总训练、验证、校准和测试数据的内容指纹及交集证据。

    比较精确文本交集，并另查同源改写分组；没有精确重复不等于没有同源泄漏。
    """
    training_path = Path(settings.risk_training_dataset)
    validation_path = Path(settings.risk_validation_dataset)
    training_texts = _jsonl_texts(training_path)
    validation_texts = _jsonl_texts(validation_path)
    calibration_texts = {_row_text(row) for row in calibration_rows}
    test_texts = {_row_text(row) for row in test_rows}
    groups_verified = _source_groups_verified(
        Path(settings.risk_calibration_split),
        calibration_rows,
        test_rows,
    )
    named = {
        "training": training_texts,
        "validation": validation_texts,
        "calibration": calibration_texts,
        "test": test_texts,
    }
    overlaps = {
        f"{left}-{right}": len(named[left].intersection(named[right]))
        for index, left in enumerate(named)
        for right in list(named)[index + 1 :]
    }
    return {
        "training": _split_descriptor(training_path, len(training_texts)),
        "validation": _split_descriptor(validation_path, len(validation_texts)),
        "calibration": _split_descriptor(
            Path(settings.risk_calibration_dataset),
            len(calibration_rows),
        ),
        "test": _split_descriptor(
            Path(settings.risk_calibration_test_dataset),
            len(test_rows),
        ),
        "exactTextOverlap": overlaps,
        "sourceGroupsVerified": groups_verified,
    }


def _jsonl_texts(path: Path) -> set[str]:
    """读取逐行样本文件中的 input 文本并去重。

    文件缺失时返回空集合，空行跳过；已有文件格式错误仍会抛出。
    """
    if not path.exists():
        return set()
    return {
        str(json.loads(line).get("input", "")).strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def _row_text(row: dict) -> str:
    """为样本提取用于交集检查的文本标识。

    优先取 text，缺少时退到 id，再去除首尾空白。
    """
    return str(row.get("text") or row.get("id") or "").strip()


def _split_descriptor(path: Path, cases: int) -> dict:
    """记录一个数据文件的路径、案例数和内容指纹。

    文件不存在时明确标为 missing，不把缺失文件当作正常空数据版本。
    """
    return {
        "path": str(path),
        "cases": cases,
        "version": hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing",
    }


def _source_groups_verified(
    path: Path,
    calibration_rows: list[dict],
    test_rows: list[dict],
) -> bool:
    """检查校准与测试样本是否都有来源分组且两组互不重叠。

    分组文件缺失、结构不符或任一样本无分组都返回 False。
    """
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    groups = data.get("sourceGroups")
    if not isinstance(groups, dict):
        return False
    calibration_groups = {groups.get(row["id"]) for row in calibration_rows}
    test_groups = {groups.get(row["id"]) for row in test_rows}
    return None not in calibration_groups.union(test_groups) and calibration_groups.isdisjoint(
        test_groups
    )


if __name__ == "__main__":
    result = evaluate_calibration()
    print(json.dumps({key: value for key, value in result.items() if key != "cases"}, ensure_ascii=False, indent=2))
