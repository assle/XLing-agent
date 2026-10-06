from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, Field, ValidationError

from app.core import diagnostics
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient

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
# 四维提取的字段约束。
# ---------------------------------------------------------------------------

class CBTExtractionSchema(BaseModel):
    """Schema for extracting the four cognitive-behavioral aspects."""
    trigger_event: Optional[str] = Field(None, description="触发事件：发生了什么事")
    thoughts: Optional[str] = Field(None, description="想法：用户的念头和认知")
    body_reactions: Optional[str] = Field(None, description="身体反应：生理症状")
    behavior: Optional[str] = Field(None, description="行为：用户的应对方式")


# ---------------------------------------------------------------------------
# 跨消息累计的四维追问状态。
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
        """判断触发事件、想法、身体反应和行为是否都已有非空内容。

        只检查四个方面的信息，不受暂停标志影响。
        """
        return all(getattr(self, dim) for dim in DIMENSIONS)

    @property
    def next_dimension(self) -> Optional[str]:
        """按预设顺序找出第一个尚未填写的方面。

        找到则返回内部字段名；四个方面都已填写时返回 None，供后续转入行动计划。
        """
        for dim in DIMENSIONS:
            if not getattr(self, dim):
                return dim
        return None

    @property
    def completed_count(self) -> int:
        """统计当前已有非空信息的方面数量。

        返回零到四之间的整数，用于显示追问进度。
        """
        return sum(1 for dim in DIMENSIONS if getattr(self, dim))

    def to_dict(self) -> dict:
        """导出四维内容、暂停状态及由内容计算的进度信息。

        返回新字典，供会话缓存和网页事件使用，不修改当前状态。
        """
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
        """从保存的字典恢复四维内容和暂停标志。

        缺失的方面保持为空，缺少暂停标志时默认未暂停；完成数等派生值会重新计算。
        """
        return cls(
            trigger_event=data.get("trigger_event"),
            thoughts=data.get("thoughts"),
            body_reactions=data.get("body_reactions"),
            behavior=data.get("behavior"),
            paused=data.get("paused", False),
        )


# ---------------------------------------------------------------------------
# 服务实现。
# ---------------------------------------------------------------------------

class CBTService:
    """通过模型提取与固定追问配合，逐步了解触发事件、想法、身体反应和行为。"""

    def __init__(self, ai: AiClient | None = None):
        """记录可选模型客户端，供四维信息提取使用。

        没有客户端时仍可使用关键词规则进行有限提取。
        """
        self.ai = ai

    def extract_dimensions(self, user_input: str, current_state: CBTState) -> CBTState:
        """从本次用户表达中补充尚未覆盖的四维信息。

        user_input 是本次原文，current_state 是此前累计结果；返回新状态，保留已有内容。
        模型不可用或调用、解析失败时改用关键词规则，不中断整个支持流程。
        """
        # 没有模型客户端时直接采用关键词规则，仍允许用户逐步补齐四个方面。
        with diagnostics.stage("interview.extract"):
            if not self.ai:
                return self._heuristic_extract(user_input, current_state)

            messages = self._extraction_prompt(user_input, current_state)
            try:
                raw = self.ai.complete(messages)
                schema = self._parse_extraction(raw)
                # 解析结果只用于填空，合并函数保留已有非空信息。
                return self._merge(current_state, schema)
            except (json.JSONDecodeError, ValidationError) as exc:
                diagnostics.degraded("interview.extract", "invalid_interview_extraction", exc)
                return self._heuristic_extract(user_input, current_state)
            except Exception as exc:
                diagnostics.degraded("interview.extract", "interview_model_unavailable", exc)
                return self._heuristic_extract(user_input, current_state)

    def get_next_question(self, state: CBTState) -> str:
        """针对第一个缺失方面返回固定追问语句。

        四个方面齐全时返回转向行动计划的说明；此处不生成新的模型回复。
        """
        dim = state.next_dimension
        if dim is None:
            return "我已经了解了你的情况。接下来我们可以一起制定一个具体的行动计划。"

        base_question = DIMENSION_QUESTIONS[dim]
        # 当前直接返回该缺失方面的固定问题，不再加入备考阶段背景。
        return base_question

    # 将已覆盖信息和本次用户表达组装成四维信息提取请求。
    # 返回要求结构化回答的消息列表，不执行模型调用；已覆盖内容用于给模型提供上下文。
    # 本函数源码参与提示词版本计算，因此保留函数内部原文。
    def _extraction_prompt(self, user_input: str, current_state: CBTState) -> list[AiMessage]:
        covered = {dim: getattr(current_state, dim) for dim in DIMENSIONS if getattr(current_state, dim)}
        return [
            AiMessage(role="system", content=(
        "你是一个认知行为四维追问助手。从用户的回答中提取四个方面的信息。"
                "只返回严格 JSON："
                '{"trigger_event":"触发事件描述或null","thoughts":"想法描述或null",'
                '"body_reactions":"身体反应描述或null","behavior":"行为描述或null"}'
                "\n只填写用户回答中明确涉及的维度，未涉及的填 null。"
            )),
            AiMessage(role="user", content=(
                f"已覆盖维度：{json.dumps(covered, ensure_ascii=False)}\n"
                f"用户回答：{user_input}"
            )),
        ]

    @staticmethod
    def _parse_extraction(raw: str) -> CBTExtractionSchema:
        """截取模型回复中的对象文本并校验四维字段。

        无法解析或字段类型不符合约定时抛错，交由外层采用关键词提取。
        """
        start = raw.find("{")
        end = raw.rfind("}")
        json_str = raw[start:end + 1] if start >= 0 and end > start else raw
        data = json.loads(json_str)
        return CBTExtractionSchema(**data)

    @staticmethod
    def _merge(current: CBTState, schema: CBTExtractionSchema) -> CBTState:
        """把本次提取结果填入已有状态的空缺方面。

        已有非空内容优先，暂停标志保持原值；返回新对象，不原地改写传入状态。
        """
        # 各字段优先取现有内容；返回新状态，避免直接修改调用方仍在使用的对象。
        return CBTState(
            trigger_event=current.trigger_event or schema.trigger_event,
            thoughts=current.thoughts or schema.thoughts,
            body_reactions=current.body_reactions or schema.body_reactions,
            behavior=current.behavior or schema.behavior,
            paused=current.paused,
        )

    @staticmethod
    def _heuristic_extract(user_input: str, current: CBTState) -> CBTState:
        """在模型不可用时按关键词为四个方面补充候选内容。

        先复制已有状态，再逐个检查尚为空的方面；命中时保存用户原文前 100 个字符。
        这是粗略的关键词匹配，不代表准确提取了独立事实，也不会覆盖已有内容。
        """
        text = user_input.lower()
        result = CBTState(
            trigger_event=current.trigger_event,
            thoughts=current.thoughts,
            body_reactions=current.body_reactions,
            behavior=current.behavior,
            paused=current.paused,
        )
        # 每个方面只在尚未填写时尝试补充；同一句表达可能同时命中多个方面。
        if not result.trigger_event:
            event_keywords = ["考试", "复习", "面试", "作业", "deadline", "考研", "考公", "成绩", "工作", "加班", "家庭", "关系", "失业"]
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
