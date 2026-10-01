from __future__ import annotations

import json
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, Field, ValidationError
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core import diagnostics
from app.core.enums import EmotionLabel, RiskLevel
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient, PromptTemplates, has_consult_signal, has_high_risk_signal


def _record_retry(state: RetryCallState) -> None:
    exception = state.outcome.exception() if state.outcome is not None else None
    diagnostics.degraded("assessment", "assessment_retry", exception, attempt=state.attempt_number)


# ---------------------------------------------------------------------------
# 结构化评估输出的字段和取值约束。
# ---------------------------------------------------------------------------

class RiskAssessmentSchema(BaseModel):
    """Schema for LLM risk assessment structured output."""
    emotion: EmotionLabel
    emotionScore: float = Field(ge=0.0, le=5.0)
    risk: RiskLevel
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = Field(min_length=1)


class TransientAssessmentError(Exception):
    """Retryable error: bad format, network timeout, or transient provider error."""


class PermanentAssessmentError(Exception):
    """Non-retryable error: auth failure, bad request, or other 4xx."""


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
    def __init__(
        self,
        ai: AiClient,
        max_retries: int = 3,
    ):
        """保存模型客户端和结构化评估辅助路径的尝试次数。

        当前 assess 与 aassess 主入口使用分类器路径；重试参数供独立的结构化评估方法使用。
        """
        self.ai = ai
        self.max_retries = max_retries

    def assess(self, text: str, history: list[AiMessage] | None = None) -> PsychologyAssessment:
        # 明确高风险关键词优先，直接跳过分类器。
        """同步完成明确风险检查、分类标签映射和保守回退。

        text 是当前文本；history 参数保留在接口中，但当前分类路径不使用它。
        明确高风险表达直接返回高风险，未知标签或分类异常返回保守结果。
        """
        # 第一层直接检查明确风险词，命中后不依赖模型服务是否可用。
        with diagnostics.stage("assessment"):
            if has_high_risk_signal(text):
                return PsychologyAssessment(EmotionLabel.HIGH_RISK, 4.0, RiskLevel.HIGH, 0.95, "检测到明确高风险表达")
            # 分类器只返回中文标签，再按规则补齐评估字段。
            try:
                # 第二层只取得分类标签，再由固定规则补成评估，不解析旧式自由生成的评估对象。
                label = self.ai.classify(text)
                assessment = assessment_from_label(label)
                if assessment is not None:
                    return assessment
                diagnostics.degraded("assessment", "unknown_classifier_label")
            except Exception as exc:
                diagnostics.degraded("assessment", "classifier_unavailable", exc)
            # 无法取得可靠标签时使用保守评估。
            # 未知标签或调用失败都不能当作安全，返回明确带不确定性的保守评估。
            return safe_fallback_assessment()

    async def aassess(self, text: str, history: list[AiMessage] | None = None) -> PsychologyAssessment:
        # 明确高风险关键词优先，直接跳过分类器。
        """异步完成与同步入口相同的分类评估流程。

        等待分类器时让出执行机会；明确风险规则无需等待模型，未知标签或异常使用保守结果。
        """
        with diagnostics.stage("assessment"):
            if has_high_risk_signal(text):
                return PsychologyAssessment(EmotionLabel.HIGH_RISK, 4.0, RiskLevel.HIGH, 0.95, "检测到明确高风险表达")
            # 分类器只返回中文标签，再按规则补齐评估字段。
            try:
                # 异步等待专用分类器，分类失败仍保留后面的保守备用结果。
                label = await self.ai.aclassify(text)
                assessment = assessment_from_label(label)
                if assessment is not None:
                    return assessment
                diagnostics.degraded("assessment", "unknown_classifier_label")
            except Exception as exc:
                diagnostics.degraded("assessment", "classifier_unavailable", exc)
            # 无法取得可靠标签时使用保守评估。
            return safe_fallback_assessment()

    # ------------------------------------------------------------------
    # 保留的结构化评估辅助入口及重试策略。
    # ------------------------------------------------------------------

    def _assess_with_retry(self, text: str, history: list[AiMessage]) -> PsychologyAssessment:
        """为结构化评估调用添加有限次数的同步重试。

        只重试 TransientAssessmentError，等待时间逐次增加并限制上限；次数耗尽后继续抛出异常。
        """
        # 装饰器控制尝试次数和等待，只有标记为暂时性的错误会再次执行内部函数。
        @retry(
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
            retry=retry_if_exception_type(TransientAssessmentError),
            before_sleep=_record_retry,
            reraise=True,
        )
        def _attempt() -> PsychologyAssessment:
            """执行一次结构化评估请求并解析结果。

            外层重试装饰器负责再次尝试，本函数只代表单次调用，不捕获最终错误。
            """
            return self._call_and_parse(text, history)
        return _attempt()

    async def _aassess_with_retry(self, text: str, history: list[AiMessage]) -> PsychologyAssessment:
        """为结构化评估提供异步重试入口。

        text 和 history 传给每次请求，只对暂时性错误重试；等待与请求均使用异步方式。
        """
        @retry(
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
            retry=retry_if_exception_type(TransientAssessmentError),
            before_sleep=_record_retry,
            reraise=True,
        )
        async def _attempt() -> PsychologyAssessment:
            """异步执行一次结构化评估请求。

            保留错误类型给外层重试策略判断，不自行返回保守结果。
            """
            return await self._acall_and_parse(text, history)
        return await _attempt()

    # ------------------------------------------------------------------
    # 模型调用、错误分类与结构校验。
    # ------------------------------------------------------------------

    def _call_and_parse(self, text: str, history: list[AiMessage]) -> PsychologyAssessment:
        """同步调用结构化评估模型，并把请求错误区分为可重试和不可重试。

        超时、网络错误、限流和服务端错误归为暂时性错误；其他请求状态错误归为永久错误。
        请求成功后校验输出，格式错误由校验函数转换。
        """
        try:
            raw = self.ai.complete(PromptTemplates.psychology_prompt(history, text))
        except httpx.TimeoutException as exc:
            raise TransientAssessmentError(f"LLM timeout: {type(exc).__name__}") from exc
        except httpx.NetworkError as exc:
            raise TransientAssessmentError(f"LLM network error: {type(exc).__name__}") from exc
        except httpx.HTTPStatusError as exc:
            # 限流和服务端错误允许重试，普通客户端错误按不可重试处理。
            if exc.response.status_code == 429 or exc.response.status_code >= 500:
                raise TransientAssessmentError(f"LLM retryable status {exc.response.status_code}") from exc
            raise PermanentAssessmentError(f"LLM permanent status {exc.response.status_code}") from exc
        except Exception as exc:
            raise TransientAssessmentError(f"LLM unexpected error: {type(exc).__name__}") from exc
        return self._validate(raw)

    async def _acall_and_parse(self, text: str, history: list[AiMessage]) -> PsychologyAssessment:
        """异步请求结构化评估并应用与同步入口一致的错误分类。

        成功后复用同一校验函数，避免两种调用方式产生不同的风险规则。
        """
        try:
            raw = await self.ai.acomplete(PromptTemplates.psychology_prompt(history, text))
        except httpx.TimeoutException as exc:
            raise TransientAssessmentError(f"LLM timeout: {type(exc).__name__}") from exc
        except httpx.NetworkError as exc:
            raise TransientAssessmentError(f"LLM network error: {type(exc).__name__}") from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429 or exc.response.status_code >= 500:
                raise TransientAssessmentError(f"LLM retryable status {exc.response.status_code}") from exc
            raise PermanentAssessmentError(f"LLM permanent status {exc.response.status_code}") from exc
        except Exception as exc:
            raise TransientAssessmentError(f"LLM unexpected error: {type(exc).__name__}") from exc
        return self._validate(raw)

    @staticmethod
    def _validate(raw: str) -> PsychologyAssessment:
        """解析模型的结构化评估结果并校验字段和取值范围。

        当分数对应风险高于模型标签时采用更高等级，高风险情绪标签也强制使用高风险。
        解析或结构校验失败转为暂时性评估错误，供辅助重试路径处理。
        """
        start = raw.find("{")
        end = raw.rfind("}")
        json_str = raw[start:end + 1] if start >= 0 and end > start else raw
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as exc:
            raise TransientAssessmentError(f"JSON parse error: {exc}") from exc
        try:
            schema = RiskAssessmentSchema(**data)
        except ValidationError as exc:
            raise TransientAssessmentError(f"Schema validation error: {exc}") from exc
        # 交叉核对分数和情绪标签，只提高已有风险保护级别。
        risk = schema.risk
        # 标签与分数不一致时只提高风险，不用较低分数降低已有保护级别。
        score_risk = risk_from_score(schema.emotionScore)
        if risk_order(score_risk) > risk_order(risk):
            risk = score_risk
        if schema.emotion == EmotionLabel.HIGH_RISK:
            risk = RiskLevel.HIGH
        return PsychologyAssessment(
            schema.emotion, schema.emotionScore, risk, schema.confidence, schema.summary
        )


# ---------------------------------------------------------------------------
# 不调用模型的独立关键词辅助规则。
# ---------------------------------------------------------------------------

def heuristic(text: str) -> PsychologyAssessment:
    """仅根据心理支持及低落关键词构造粗略评估。

    返回规则定义的固定分数和置信度；它是独立辅助规则，不等同于主入口的完整高风险检查。
    """
    if has_consult_signal(text):
        if any(word in text.lower() for word in ["抑郁", "低落", "崩溃", "难过", "depress", "hopeless"]):
            return PsychologyAssessment(EmotionLabel.DEPRESSED, 3.1, RiskLevel.MEDIUM, 0.75, "检测到低落或抑郁相关表达")
        return PsychologyAssessment(EmotionLabel.ANXIETY, 2.2, RiskLevel.LOW, 0.72, "检测到焦虑或压力相关表达")
    return PsychologyAssessment(EmotionLabel.NORMAL, 0.0, RiskLevel.LOW, 0.66, "未检测到明显风险信号")


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


def risk_order(risk: RiskLevel) -> int:
    """将风险等级映射成可比较的递增整数。

    低、中、高分别为一、二、三，用于选择更保守的等级。
    """
    return {RiskLevel.LOW: 1, RiskLevel.MEDIUM: 2, RiskLevel.HIGH: 3}[risk]


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
    if emotion == EmotionLabel.HIGH_RISK:
        risk = RiskLevel.HIGH
    return PsychologyAssessment(emotion, score, risk, 0.8, f"分类器判定为{label.strip()}")
