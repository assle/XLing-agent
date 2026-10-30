from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.core import diagnostics
from app.core.config import get_settings
from app.core.enums import EmotionLabel, RiskLevel
from app.services.ai import AiClient, has_high_risk_signal
from app.services.decision_classifier import DecisionPrediction, get_decision_classifier

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
    def __init__(self, ai: AiClient, decision_classifier=None, backend: str | None = None):
        """保存模型客户端与可选的决策头分类器。

        decision_classifier 是测试注入点，生产按配置懒加载进程级共享实例；
        backend 缺省读取全局配置，decision_head 启用单次前向分层安全路径，
        其他值保持原有生成式分类路径不变。
        """
        self.ai = ai
        self._decision_classifier = decision_classifier
        self._backend = backend

    def _backend_name(self) -> str:
        """返回本次评估使用的分类后端名称。"""
        if self._backend is not None:
            return self._backend
        return get_settings().classifier_backend.lower()

    def _decision(self):
        """返回决策头分类器，未注入时获取进程级共享实例。"""
        if self._decision_classifier is None:
            self._decision_classifier = get_decision_classifier()
        return self._decision_classifier

    def assess(self, text: str) -> PsychologyAssessment:
        """同步完成明确风险检查、分类标签映射和保守回退。

        明确高风险表达直接返回高风险，未知标签或分类异常返回保守结果。
        """
        with diagnostics.stage("assessment"):
            if has_high_risk_signal(text):
                return PsychologyAssessment(EmotionLabel.HIGH_RISK, 4.0, RiskLevel.HIGH, 0.95, "检测到明确高风险表达")
            if self._backend_name() == "decision_head":
                return self._assess_decision(text)
            return self._assess_ollama(text)

    async def aassess(self, text: str) -> PsychologyAssessment:
        """异步完成与同步入口相同的分类评估流程。

        等待分类器时让出执行机会；明确风险规则无需等待模型，未知标签或异常使用保守结果。
        """
        with diagnostics.stage("assessment"):
            if has_high_risk_signal(text):
                return PsychologyAssessment(EmotionLabel.HIGH_RISK, 4.0, RiskLevel.HIGH, 0.95, "检测到明确高风险表达")
            if self._backend_name() == "decision_head":
                settings = get_settings()
                try:
                    prediction = await asyncio.to_thread(self._decision().predict, text)
                except Exception as exc:
                    diagnostics.degraded("assessment", "decision_head_unavailable", exc)
                    return await self._assess_ollama_async(text)
                direct = self._decision_direct(prediction, settings)
                if direct is not None:
                    return direct
                try:
                    fallback_label = await self.ai.aclassify(text)
                except Exception as exc:
                    diagnostics.degraded("assessment", "classifier_unavailable", exc)
                    fallback_label = None
                return self._decision_with_fallback(prediction, settings, fallback_label)
            return await self._assess_ollama_async(text)

    # ------------------------------------------------------------------
    # 原生成式分类主路径；决策头不可用时也复用此路径。
    # ------------------------------------------------------------------

    def _assess_ollama(self, text: str) -> PsychologyAssessment:
        """同步调用生成式分类器并映射标签，失败返回保守评估。"""
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

    async def _assess_ollama_async(self, text: str) -> PsychologyAssessment:
        """异步调用生成式分类器并映射标签，失败返回保守评估。"""
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

    # ------------------------------------------------------------------
    # 决策头分层安全策略：高风险双阈值 + 低置信度回退 + 分歧就高。
    # ------------------------------------------------------------------

    def _assess_decision(self, text: str) -> PsychologyAssessment:
        """同步执行决策头分层评估；模型加载或推理失败退回生成式主路径。"""
        settings = get_settings()
        try:
            prediction = self._decision().predict(text)
        except Exception as exc:
            diagnostics.degraded("assessment", "decision_head_unavailable", exc)
            return self._assess_ollama(text)
        direct = self._decision_direct(prediction, settings)
        if direct is not None:
            return direct
        try:
            fallback_label = self.ai.classify(text)
        except Exception as exc:
            diagnostics.degraded("assessment", "classifier_unavailable", exc)
            fallback_label = None
        return self._decision_with_fallback(prediction, settings, fallback_label)

    def _decision_direct(self, prediction: DecisionPrediction, settings) -> PsychologyAssessment | None:
        """无需复核即可判定的分支：达到高风险阈值，或高置信且不在高风险灰区。

        返回 None 表示需要回退生成式分类器复核。
        """
        if prediction.high_risk_probability >= settings.classifier_high_risk_threshold:
            return self._decision_result(
                "高风险", prediction.high_risk_probability, prediction,
                "决策头高风险判定", fallback_used=False,
            )
        gray = prediction.high_risk_probability >= settings.classifier_high_risk_review_threshold
        if (
            gray
            or prediction.confidence < settings.classifier_low_confidence_threshold
            or prediction.margin < settings.classifier_margin_threshold
        ):
            return None
        return self._decision_result(
            prediction.label, prediction.confidence, prediction,
            "决策头高置信判定", fallback_used=False,
        )

    def _decision_with_fallback(
        self, prediction: DecisionPrediction, settings, fallback_label: str | None,
    ) -> PsychologyAssessment:
        """低置信或灰区样本的复核分支。

        复核不可用：灰区按保守高风险处理，其余沿用保守中风险评估；
        复核一致：采用决策头标签；复核分歧：按严重度就高，不把分歧解释为低风险。
        """
        gray = prediction.high_risk_probability >= settings.classifier_high_risk_review_threshold
        if fallback_label is None or label_to_emotion(fallback_label) is None:
            if fallback_label is not None:
                diagnostics.degraded(
                    "assessment", "unknown_classifier_label",
                    status="empty_output" if not fallback_label.strip() else "non_label_output",
                )
            if gray:
                return self._decision_result(
                    "高风险", 0.4, prediction,
                    "高风险灰区且复核不可用，按保守高风险处理", fallback_used=True,
                )
            return safe_fallback_assessment()
        fallback_label = fallback_label.strip()
        if fallback_label == prediction.label:
            return self._decision_result(
                prediction.label, prediction.confidence, prediction,
                "低置信回退，与生成式复核一致", fallback_used=True,
            )
        chosen = self._more_conservative(prediction.label, fallback_label)
        return self._decision_result(
            chosen, min(prediction.confidence, 0.5), prediction,
            f"决策分歧（决策头{prediction.label}/复核{fallback_label}），按保守标签{chosen}",
            fallback_used=True,
        )

    @staticmethod
    def _more_conservative(first: str, second: str) -> str:
        """在两个合法标签中取规则严重度更高者；同分保留决策头标签。"""
        first_score = score_for_emotion(label_to_emotion(first))
        second_score = score_for_emotion(label_to_emotion(second))
        return first if first_score >= second_score else second

    def _decision_result(
        self, label: str, confidence: float, prediction: DecisionPrediction,
        reason: str, fallback_used: bool,
    ) -> PsychologyAssessment:
        """把分层决策结果映射为业务评估对象，并记录完整决策事件供审计。"""
        emotion = label_to_emotion(label)
        score = score_for_emotion(emotion)
        diagnostics.emit(
            "assessment.decision_head",
            label=label,
            confidence=round(confidence, 6),
            high_risk_probability=round(prediction.high_risk_probability, 6),
            margin=round(prediction.margin, 6),
            fallback_used=fallback_used,
            reason=reason,
        )
        return PsychologyAssessment(
            emotion, score, risk_from_score(score), confidence,
            f"{reason}（{label}，高风险概率{prediction.high_risk_probability:.3f}）",
            model_version=prediction.model_version,
        )


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
