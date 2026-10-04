from __future__ import annotations

from dataclasses import dataclass

from app.core import diagnostics
from app.core.enums import EmotionLabel, RiskLevel
from app.services.ai import AiClient, has_high_risk_signal

# ---------------------------------------------------------------------------
# 业务使用的安全评估对象。
# ---------------------------------------------------------------------------

@dataclass
class PsychologyAssessment:
    emotion: EmotionLabel
    emotion_score: float
    risk: RiskLevel
    confidence: float
    summary: str
    model_version: str = ""


def safe_fallback_assessment() -> PsychologyAssessment:
    """在分类不可用时返回保守的安全风险评估结果。

    使用中风险及较低置信度表示不确定性，避免把模型失败解释成没有风险。
    """
    return PsychologyAssessment(
        EmotionLabel.ANXIETY, 2.5, RiskLevel.MEDIUM, 0.3,
        "模型评估不可用，已采用保守安全评估",
    )


# ---------------------------------------------------------------------------
# 服务实现。
# ---------------------------------------------------------------------------

class PsychologicalAssessmentService:
    def __init__(self, ai: AiClient):
        """保存用于安全风险分类的模型客户端。"""
        self.ai = ai

    def assess(self, text: str) -> PsychologyAssessment:
        """同步完成明确风险检查、分类标签映射和保守回退。

        明确高风险表达直接返回高风险，未知标签或分类异常返回保守结果。
        """
        with diagnostics.stage("assessment"):
            if has_high_risk_signal(text):
                return PsychologyAssessment(EmotionLabel.HIGH_RISK, 4.0, RiskLevel.HIGH, 0.95, "检测到明确高风险表达")
            try:
                label = self.ai.classify(text)
                assessment = assessment_from_label(label)
                if assessment is not None:
                    return assessment
                diagnostics.degraded(
                    "assessment", "unknown_classifier_label",
                    status="empty_output" if not label.strip() else "non_label_output",
                )
            except Exception as exc:
                diagnostics.degraded("assessment", "classifier_unavailable", exc)
            # 未知标签或调用失败都不能当作安全，返回明确带不确定性的保守评估。
            return safe_fallback_assessment()

    async def aassess(self, text: str) -> PsychologyAssessment:
        """异步完成与同步入口相同的分类评估流程。

        等待分类器时让出执行机会；明确风险规则无需等待模型，未知标签或异常使用保守结果。
        """
        with diagnostics.stage("assessment"):
            if has_high_risk_signal(text):
                return PsychologyAssessment(EmotionLabel.HIGH_RISK, 4.0, RiskLevel.HIGH, 0.95, "检测到明确高风险表达")
            try:
                label = await self.ai.aclassify(text)
                assessment = assessment_from_label(label)
                if assessment is not None:
                    return assessment
                diagnostics.degraded(
                    "assessment", "unknown_classifier_label",
                    status="empty_output" if not label.strip() else "non_label_output",
                )
            except Exception as exc:
                diagnostics.degraded("assessment", "classifier_unavailable", exc)
            return safe_fallback_assessment()


def score_for_emotion(emotion: EmotionLabel) -> float:
    """把已知情绪标签映射为固定的规则分数。

    分数来自内部对应表，不是量表得分，也不是模型给出的连续概率。
    """
    return {
        EmotionLabel.HIGH_RISK: 4.0,
        EmotionLabel.DEPRESSED: 3.0,
        EmotionLabel.ANXIETY: 2.0,
        EmotionLabel.NORMAL: 0.0,
    }[emotion]


def risk_from_score(score: float) -> RiskLevel:
    """按内部分数阈值划分风险等级。

    分数至少为四时为高风险，至少为三时为中风险，其他情况为低风险。
    """
    if score >= 4:
        return RiskLevel.HIGH
    if score >= 3:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


# ---------------------------------------------------------------------------
# 当前分类器主路径使用的中文标签映射。
# ---------------------------------------------------------------------------

_LABEL_TO_EMOTION = {
    "正常": EmotionLabel.NORMAL,
    "焦虑": EmotionLabel.ANXIETY,
    "低落": EmotionLabel.DEPRESSED,
    "高风险": EmotionLabel.HIGH_RISK,
}


def label_to_emotion(label: str) -> EmotionLabel | None:
    """将去除首尾空白的中文分类标签映射成内部枚举。

    未识别的文本返回 None，让调用方明确进入备用处理。
    """
    return _LABEL_TO_EMOTION.get(label.strip())


def assessment_from_label(label: str) -> PsychologyAssessment | None:
    """根据已知中文分类标签构造完整的规则评估结果。

    分数和风险通过固定规则补齐，置信度固定为 0.8；未知标签返回 None，不猜测含义。
    """
    emotion = label_to_emotion(label)
    if emotion is None:
        return None
    score = score_for_emotion(emotion)
    risk = risk_from_score(score)
    return PsychologyAssessment(emotion, score, risk, 0.8, f"分类器判定为{label.strip()}")
