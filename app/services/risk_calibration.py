from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass

from app.core.enums import RiskLevel

RISK_LEVELS = (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH)


@dataclass(frozen=True)
class TemperatureScaler:
    temperature: float

    @classmethod
    def fit(
        cls,
        logits: list[list[float]],
        labels: list[int],
    ) -> TemperatureScaler:
        """在预设的 301 个温度候选中选择平均预测损失最小的一项。

        logits 是模型各类别原始分数，labels 是真实类别编号；要求非空且数量一致。
        温度用于调整概率分布的尖锐程度，此处是有限网格搜索而非持续训练模型。
        """
        if not logits or len(logits) != len(labels):
            raise ValueError("校准 logits 与标签必须非空且数量一致")
        candidates = [math.exp(-3.0 + index * 7.0 / 300.0) for index in range(301)]
        return min(
            (cls(temperature) for temperature in candidates),
            # 计算每个候选温度的平均损失，选择损失最小的配置。
            key=lambda scaler: scaler.negative_log_likelihood(logits, labels),
        )

    def probabilities(self, logits: list[float]) -> list[float]:
        """把一行类别原始分数转换为归一化概率。

        先除以正温度，再减去最大值后求指数，降低大数溢出的风险；输出顺序与输入类别一致。
        """
        if self.temperature <= 0:
            raise ValueError("temperature 必须大于 0")
        scaled = [value / self.temperature for value in logits]
        # 指数计算前减去共同最大值不会改变最终比例，但可降低溢出风险。
        offset = max(scaled)
        exponentials = [math.exp(value - offset) for value in scaled]
        denominator = sum(exponentials)
        return [value / denominator for value in exponentials]

    def negative_log_likelihood(
        self,
        logits: list[list[float]],
        labels: list[int],
    ) -> float:
        """计算模型给真实类别分配概率的平均损失。

        每行使用对应标签取概率，概率下限限制为极小正数，避免对零取对数。
        """
        losses = []
        for row, label in zip(logits, labels, strict=True):
            probability = self.probabilities(row)[label]
            losses.append(-math.log(max(probability, 1e-12)))
        return sum(losses) / max(1, len(losses))


@dataclass(frozen=True)
class ClassConditionalConformalPredictor:
    alpha: float
    thresholds: dict[RiskLevel, float]

    @classmethod
    def fit(
        cls,
        probabilities: list[list[float]],
        labels: list[int],
        *,
        alpha: float,
    ) -> ClassConditionalConformalPredictor:
        """分别用各风险类别的校准样本计算候选集合阈值。

        alpha 是允许误覆盖程度的参数，必须在零和一之间；每类都必须有样本。
        按真实类别的 1-概率排序，再按有限样本修正后的名次选择阈值。
        """
        if not 0 < alpha < 1:
            raise ValueError("alpha 必须在 0 和 1 之间")
        if not probabilities or len(probabilities) != len(labels):
            raise ValueError("校准概率与标签必须非空且数量一致")
        thresholds = {}
        for label_index, risk in enumerate(RISK_LEVELS):
            scores = sorted(
                1.0 - row[label_index]
                for row, expected in zip(probabilities, labels, strict=True)
                if expected == label_index
            )
            if not scores:
                raise ValueError(f"校准集缺少 {risk.value} 样本")
            # 按每类自己的样本量选择有限样本修正后的阈值位置，不把不同类别混在一起。
            rank = math.ceil((len(scores) + 1) * (1.0 - alpha))
            thresholds[risk] = scores[min(rank, len(scores)) - 1]
        return cls(alpha=alpha, thresholds=thresholds)

    def predict_set(self, probabilities: list[float]) -> tuple[RiskLevel, ...]:
        """返回与校准阈值相容的所有风险等级。

        某类的 1-概率不超过该类阈值时纳入集合；若没有类别符合，则返回全部类别以明确表达不确定性。
        """
        included = tuple(
            risk
            for index, risk in enumerate(RISK_LEVELS)
            if 1.0 - probabilities[index] <= self.thresholds[risk]
        )
        # 没有类别满足阈值时，输入不能被当前校准规则明确归类。
        # 返回所有类别表达不确定性，同时保留高风险保护分支。
        return included or RISK_LEVELS


@dataclass(frozen=True)
class RiskPrediction:
    raw_probabilities: dict[str, float]
    probabilities: dict[str, float]
    prediction_set: tuple[RiskLevel, ...]
    selected_risk: RiskLevel
    uncertain: bool
    requires_review: bool
    model_version: str
    calibration_version: str


class CalibratedRiskEngine:
    def __init__(
        self,
        scaler: TemperatureScaler,
        conformal: ClassConditionalConformalPredictor,
        *,
        model_version: str,
    ) -> None:
        """组合概率温度调整和按类别计算的候选集合规则。

        保存模型版本，并按实际校准参数计算校准版本指纹。
        """
        self.scaler = scaler
        self.conformal = conformal
        self.model_version = model_version
        self.calibration_version = _calibration_version(scaler, conformal)

    def predict(
        self,
        logits: list[float],
        *,
        explicit_high_risk: bool = False,
    ) -> RiskPrediction:
        """将原始分数转换为概率、候选风险集合及保守选择结果。

        明确高风险信号直接固定为高风险；其他情况使用校准概率并选择集合中最高风险。
        集合含多个等级表示不确定，包含高风险即标记需要人工审核。
        """
        prediction_set: tuple[RiskLevel, ...]
        if explicit_high_risk:
            raw_probabilities = [0.0, 0.0, 1.0]
            probabilities = [0.0, 0.0, 1.0]
            prediction_set = (RiskLevel.HIGH,)
        else:
            raw_probabilities = TemperatureScaler(1.0).probabilities(logits)
            probabilities = self.scaler.probabilities(logits)
            prediction_set = self.conformal.predict_set(probabilities)
        # 候选有多个等级时取最高者，避免不确定性被错误解释为低风险。
        selected_risk = max(prediction_set, key=RISK_LEVELS.index)
        uncertain = len(prediction_set) != 1
        return RiskPrediction(
            raw_probabilities={
                risk.value: raw_probabilities[index]
                for index, risk in enumerate(RISK_LEVELS)
            },
            probabilities={
                risk.value: probabilities[index]
                for index, risk in enumerate(RISK_LEVELS)
            },
            prediction_set=prediction_set,
            selected_risk=selected_risk,
            uncertain=uncertain,
            requires_review=RiskLevel.HIGH in prediction_set,
            model_version=self.model_version,
            calibration_version=self.calibration_version,
        )

    def to_dict(self) -> dict:
        """导出温度、分类阈值和模型及校准版本。

        返回可保存成文本对象的基础字段，不写入文件。
        """
        return {
            "temperature": self.scaler.temperature,
            "alpha": self.conformal.alpha,
            "thresholds": {
                risk.value: threshold
                for risk, threshold in self.conformal.thresholds.items()
            },
            "modelVersion": self.model_version,
            "calibrationVersion": self.calibration_version,
        }



def _calibration_version(
    scaler: TemperatureScaler,
    conformal: ClassConditionalConformalPredictor,
) -> str:
    """对排序规范化后的校准参数计算内容指纹。

    仅温度、alpha 和每类阈值参与，便于判断校准规则是否变化。
    """
    payload = {
        "temperature": scaler.temperature,
        "alpha": conformal.alpha,
        "thresholds": {
            risk.value: conformal.thresholds[risk]
            for risk in RISK_LEVELS
        },
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
