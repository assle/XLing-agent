from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from pydantic import BaseModel, Field, ValidationError

from app.schemas.dtos import AiMessage
from app.services.ai import AiClient, PromptTemplates

logger = logging.getLogger(__name__)

DIMENSIONS = ["trigger_event", "thoughts", "body_reactions", "behavior"]
DIMENSION_LABELS = {
    "trigger_event": "触发事件",
    "thoughts": "想法",
    "body_reactions": "身体反应",
    "behavior": "行为",
}
DIMENSION_QUESTIONS = {
    "trigger_event": "最近发生了什么事让你感到焦虑？能具体说说当时的情境吗？",
    "thoughts": "当时你脑海里在想些什么？有什么具体的念头或担忧吗？",
    "body_reactions": "那段时间你的身体有什么感觉？比如心跳、呼吸、肌肉紧张等。",
    "behavior": "焦虑的时候你会怎么做？有没有什么习惯性的应对方式？",
}


# ---------------------------------------------------------------------------
# Structured output schema (issue 08)
# ---------------------------------------------------------------------------

class CBTExtractionSchema(BaseModel):
    """Schema for extracting the four cognitive-behavioral aspects."""
    trigger_event: Optional[str] = Field(None, description="触发事件：发生了什么事")
    thoughts: Optional[str] = Field(None, description="想法：用户的念头和认知")
    body_reactions: Optional[str] = Field(None, description="身体反应：生理症状")
    behavior: Optional[str] = Field(None, description="行为：用户的应对方式")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class CBTState:
    trigger_event: Optional[str] = None
    thoughts: Optional[str] = None
    body_reactions: Optional[str] = None
    behavior: Optional[str] = None
    paused: bool = False

    @property
    def is_complete(self) -> bool:
        return all(getattr(self, dim) for dim in DIMENSIONS)

    @property
    def next_dimension(self) -> Optional[str]:
        for dim in DIMENSIONS:
            if not getattr(self, dim):
                return dim
        return None

    @property
    def completed_count(self) -> int:
        return sum(1 for dim in DIMENSIONS if getattr(self, dim))

    def to_dict(self) -> dict:
        return {
            "trigger_event": self.trigger_event,
            "thoughts": self.thoughts,
            "body_reactions": self.body_reactions,
            "behavior": self.behavior,
            "paused": self.paused,
            "is_complete": self.is_complete,
            "completed_count": self.completed_count,
            "next_dimension": self.next_dimension,
        }

    @classmethod
    def from_dict(cls, data: dict) -> CBTState:
        return cls(
            trigger_event=data.get("trigger_event"),
            thoughts=data.get("thoughts"),
            body_reactions=data.get("body_reactions"),
            behavior=data.get("behavior"),
            paused=data.get("paused", False),
        )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class CBTService:
    """Four-part cognitive-behavioral questioning (issue 08).

    Dimensions: triggering event, thoughts, body reactions, behavior.
    LLM extracts covered dimensions; system asks one missing dimension at a time.
    """

    def __init__(self, ai: AiClient | None = None):
        self.ai = ai

    def extract_dimensions(self, user_input: str, current_state: CBTState) -> CBTState:
        """Extract the four aspects from a student's response.

        Merges new extractions into the current state without overwriting
        already-covered dimensions.
        """
        if not self.ai:
            return self._heuristic_extract(user_input, current_state)

        messages = self._extraction_prompt(user_input, current_state)
        try:
            raw = self.ai.complete(messages)
            schema = self._parse_extraction(raw)
            return self._merge(current_state, schema)
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning("four-part extraction failed, using heuristic: %s", exc)
            return self._heuristic_extract(user_input, current_state)
        except Exception as exc:
            logger.warning("four-part extraction error, using heuristic: %s", exc)
            return self._heuristic_extract(user_input, current_state)

    def get_next_question(self, state: CBTState, exam_stage: str = "") -> str:
        """Generate the next question for the missing dimension.

        Uses the standard question template for each dimension.
        """
        dim = state.next_dimension
        if dim is None:
            return "我已经了解了你的情况。接下来我们可以一起制定一个具体的行动计划。"

        base_question = DIMENSION_QUESTIONS[dim]
        # Add stage-aware context if available
        if exam_stage:
            stage_label = DIMENSION_LABELS.get(dim, "")
            return f"{base_question}（结合你目前{exam_stage}阶段的情况）"
        return base_question

    def _extraction_prompt(self, user_input: str, current_state: CBTState) -> list[AiMessage]:
        covered = {dim: getattr(current_state, dim) for dim in DIMENSIONS if getattr(current_state, dim)}
        return [
            AiMessage(role="system", content=(
                "你是一个认知行为四维追问助手。从学生的回答中提取四个方面的信息。"
                "只返回严格 JSON："
                '{"trigger_event":"触发事件描述或null","thoughts":"想法描述或null",'
                '"body_reactions":"身体反应描述或null","behavior":"行为描述或null"}'
                "\n只填写学生回答中明确涉及的维度，未涉及的填 null。"
            )),
            AiMessage(role="user", content=(
                f"已覆盖维度：{json.dumps(covered, ensure_ascii=False)}\n"
                f"学生回答：{user_input}"
            )),
        ]

    @staticmethod
    def _parse_extraction(raw: str) -> CBTExtractionSchema:
        start = raw.find("{")
        end = raw.rfind("}")
        json_str = raw[start:end + 1] if start >= 0 and end > start else raw
        data = json.loads(json_str)
        return CBTExtractionSchema(**data)

    @staticmethod
    def _merge(current: CBTState, schema: CBTExtractionSchema) -> CBTState:
        """Merge new extractions into current state without overwriting."""
        return CBTState(
            trigger_event=current.trigger_event or schema.trigger_event,
            thoughts=current.thoughts or schema.thoughts,
            body_reactions=current.body_reactions or schema.body_reactions,
            behavior=current.behavior or schema.behavior,
            paused=current.paused,
        )

    @staticmethod
    def _heuristic_extract(user_input: str, current: CBTState) -> CBTState:
        """Keyword-based fallback extraction when LLM is unavailable."""
        text = user_input.lower()
        result = CBTState(
            trigger_event=current.trigger_event,
            thoughts=current.thoughts,
            body_reactions=current.body_reactions,
            behavior=current.behavior,
            paused=current.paused,
        )
        if not result.trigger_event:
            event_keywords = ["考试", "复习", "面试", "作业", "deadline", "考研", "考公", "成绩"]
            if any(kw in text for kw in event_keywords):
                result.trigger_event = user_input[:100]
        if not result.thoughts:
            thought_keywords = ["觉得", "认为", "担心", "害怕", "想", "怕", "感觉"]
            if any(kw in text for kw in thought_keywords):
                result.thoughts = user_input[:100]
        if not result.body_reactions:
            body_keywords = ["心跳", "失眠", "睡不着", "头痛", " stomach", "胃", "出汗", "颤抖", "胸闷", "呼吸"]
            if any(kw in text for kw in body_keywords):
                result.body_reactions = user_input[:100]
        if not result.behavior:
            behavior_keywords = ["逃避", "拖延", "刷手机", "暴饮", "暴食", "不学", "放弃", "硬撑"]
            if any(kw in text for kw in behavior_keywords):
                result.behavior = user_input[:100]
        return result
