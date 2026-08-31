from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, Field, ValidationError
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.enums import EmotionLabel, RiskLevel
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient, PromptTemplates, has_consult_signal, has_high_risk_signal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema-constrained structured output (issue 02)
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
# Domain model
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
    """Conservative fallback when structured assessment fails after retries.

    Assumes MEDIUM risk to avoid underestimating a potentially risky message.
    """
    return PsychologyAssessment(
        EmotionLabel.ANXIETY, 2.5, RiskLevel.MEDIUM, 0.3,
        "模型评估不可用，已采用保守安全评估",
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class PsychologicalAssessmentService:
    def __init__(
        self,
        ai: AiClient,
        max_retries: int = 3,
    ):
        self.ai = ai
        self.max_retries = max_retries

    def assess(self, text: str, history: list[AiMessage] | None = None) -> PsychologyAssessment:
        # Layer 1: HIGH risk keywords bypass the classifier entirely
        if has_high_risk_signal(text):
            return PsychologyAssessment(EmotionLabel.HIGH_RISK, 4.0, RiskLevel.HIGH, 0.95, "检测到明确高风险表达")
        # Layer 2: fine-tuned classifier -> Chinese label -> rule-mapped assessment
        try:
            label = self.ai.classify(text)
            assessment = assessment_from_label(label)
            if assessment is not None:
                return assessment
            logger.warning("分类器返回未知标签 %r，使用保守 fallback", label)
        except Exception:
            logger.warning("分类器调用失败，使用保守 fallback（不记录敏感正文）")
        # Layer 3: conservative fallback
        return safe_fallback_assessment()

    async def aassess(self, text: str, history: list[AiMessage] | None = None) -> PsychologyAssessment:
        # Layer 1: HIGH risk keywords bypass the classifier entirely
        if has_high_risk_signal(text):
            return PsychologyAssessment(EmotionLabel.HIGH_RISK, 4.0, RiskLevel.HIGH, 0.95, "检测到明确高风险表达")
        # Layer 2: fine-tuned classifier -> Chinese label -> rule-mapped assessment
        try:
            label = await self.ai.aclassify(text)
            assessment = assessment_from_label(label)
            if assessment is not None:
                return assessment
            logger.warning("分类器返回未知标签 %r，使用保守 fallback", label)
        except Exception:
            logger.warning("分类器调用失败，使用保守 fallback（不记录敏感正文）")
        # Layer 3: conservative fallback
        return safe_fallback_assessment()

    # ------------------------------------------------------------------
    # Structured output + retry (sync)
    # ------------------------------------------------------------------

    def _assess_with_retry(self, text: str, history: list[AiMessage]) -> PsychologyAssessment:
        @retry(
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
            retry=retry_if_exception_type(TransientAssessmentError),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )
        def _attempt() -> PsychologyAssessment:
            return self._call_and_parse(text, history)
        return _attempt()

    async def _aassess_with_retry(self, text: str, history: list[AiMessage]) -> PsychologyAssessment:
        @retry(
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
            retry=retry_if_exception_type(TransientAssessmentError),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )
        async def _attempt() -> PsychologyAssessment:
            return await self._acall_and_parse(text, history)
        return await _attempt()

    # ------------------------------------------------------------------
    # LLM call + schema validation
    # ------------------------------------------------------------------

    def _call_and_parse(self, text: str, history: list[AiMessage]) -> PsychologyAssessment:
        try:
            raw = self.ai.complete(PromptTemplates.psychology_prompt(history, text))
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

    async def _acall_and_parse(self, text: str, history: list[AiMessage]) -> PsychologyAssessment:
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
        """Parse and schema-validate LLM output."""
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
        # Cross-validate: ensure risk is consistent with emotion
        risk = schema.risk
        score_risk = risk_from_score(schema.emotionScore)
        if risk_order(score_risk) > risk_order(risk):
            risk = score_risk
        if schema.emotion == EmotionLabel.HIGH_RISK:
            risk = RiskLevel.HIGH
        return PsychologyAssessment(
            schema.emotion, schema.emotionScore, risk, schema.confidence, schema.summary
        )


# ---------------------------------------------------------------------------
# Heuristic fallback (keyword-based, no LLM)
# ---------------------------------------------------------------------------

def heuristic(text: str) -> PsychologyAssessment:
    if has_consult_signal(text):
        if any(word in text.lower() for word in ["抑郁", "低落", "崩溃", "难过", "depress", "hopeless"]):
            return PsychologyAssessment(EmotionLabel.DEPRESSED, 3.1, RiskLevel.MEDIUM, 0.75, "检测到低落或抑郁相关表达")
        return PsychologyAssessment(EmotionLabel.ANXIETY, 2.2, RiskLevel.LOW, 0.72, "检测到焦虑或压力相关表达")
    return PsychologyAssessment(EmotionLabel.NORMAL, 0.0, RiskLevel.LOW, 0.66, "未检测到明显风险信号")


def score_for_emotion(emotion: EmotionLabel) -> float:
    return {
        EmotionLabel.HIGH_RISK: 4.0,
        EmotionLabel.DEPRESSED: 3.0,
        EmotionLabel.ANXIETY: 2.0,
        EmotionLabel.NORMAL: 0.0,
    }[emotion]


def risk_from_score(score: float) -> RiskLevel:
    if score >= 4:
        return RiskLevel.HIGH
    if score >= 3:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def risk_order(risk: RiskLevel) -> int:
    return {RiskLevel.LOW: 1, RiskLevel.MEDIUM: 2, RiskLevel.HIGH: 3}[risk]


# ---------------------------------------------------------------------------
# Fine-tuned classifier path (replaces layer 2 general-LLM JSON assessment)
# ---------------------------------------------------------------------------

_LABEL_TO_EMOTION = {
    "正常": EmotionLabel.NORMAL,
    "焦虑": EmotionLabel.ANXIETY,
    "低落": EmotionLabel.DEPRESSED,
    "高风险": EmotionLabel.HIGH_RISK,
}


def label_to_emotion(label: str) -> EmotionLabel | None:
    """Map a Chinese classifier label to EmotionLabel. Returns None if unknown."""
    return _LABEL_TO_EMOTION.get(label.strip())


def assessment_from_label(label: str) -> PsychologyAssessment | None:
    """Build a full assessment from a Chinese label.

    The classifier outputs only a label word; score, risk, confidence and
    summary are filled by rules reusing score_for_emotion / risk_from_score.
    Returns None for an unrecognised label so the caller can fall back.
    """
    emotion = label_to_emotion(label)
    if emotion is None:
        return None
    score = score_for_emotion(emotion)
    risk = risk_from_score(score)
    if emotion == EmotionLabel.HIGH_RISK:
        risk = RiskLevel.HIGH
    return PsychologyAssessment(emotion, score, risk, 0.8, f"分类器判定为{label.strip()}")
