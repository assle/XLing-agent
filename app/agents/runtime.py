from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import IntentType, RiskLevel
from app.models.entities import ChatSession, UserAccount
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient
from app.services.assessment import PsychologicalAssessmentService, PsychologyAssessment
from app.services.knowledge import KnowledgeService, SearchResult
from app.services.memory import RedisShortTermMemoryStore


@dataclass
class AgentStep:
    """Human-readable log entry for one agent action.

    Observations are for humans (debugging/traces) only; structured progress
    that callers depend on travels via the typed events below."""
    step: int
    agent: str
    action: str
    observation: str


@dataclass
class CbtEvent:
    """Four-part questioning progress, constructed with its state."""
    active: bool
    completed_count: int
    next_dimension: str | None
    complete: bool


@dataclass
class ActionPlanItemEvent:
    id: int
    content: str
    order: int
    completed: bool


@dataclass
class ActionPlanEvent:
    """Freshly generated 24h action plan, constructed where the plan is created."""
    plan_id: int
    items: list[ActionPlanItemEvent]
    feedback_due_at: str | None = None


@dataclass
class AgentRunResult:
    intent: IntentType
    risk_level: RiskLevel
    assessment: PsychologyAssessment | None
    retrieved_knowledge: list[SearchResult]
    response_messages: list[AiMessage]
    steps: list[AgentStep]
    pending_review: bool = False
    fallback_response: str | None = None
    degraded: bool = False
    trajectory_rising: bool = False
    trajectory_trend: str = ""
    cbt_event: CbtEvent | None = None
    action_plan_event: ActionPlanEvent | None = None
    quick_safety_checked: bool = False
    quick_risk_flagged: bool = False

    @property
    def requires_report(self) -> bool:
        """判断本轮消息是否需要安全评估记录。

        只要消息类型不是日常对话就返回 True；实际保存还会检查是否存在评估结果。
        """
        return self.intent != IntentType.CHAT


@dataclass(frozen=True)
class AgentRuntimeDependencies:
    """Business collaborators used by the LangGraph runtime."""

    ai: AiClient
    knowledge: KnowledgeService
    memory: RedisShortTermMemoryStore
    assessment: PsychologicalAssessmentService

    @classmethod
    def create(cls, db: Session, settings: Settings) -> AgentRuntimeDependencies:
        """按配置创建模型、知识、短期记忆和评估服务。

        评估服务复用同一个模型客户端，知识服务使用传入数据库；返回可整体注入执行器的依赖对象。
        """
        ai = AiClient(settings)
        return cls(
            ai=ai,
            knowledge=KnowledgeService(db, settings),
            memory=RedisShortTermMemoryStore(settings),
            assessment=PsychologicalAssessmentService(ai),
        )


class AgentRuntime(Protocol):
    """Business boundary for conversation planning, independent of graph scheduling."""

    async def run(
        self, user: UserAccount, session: ChatSession, model_input: str,
    ) -> AgentRunResult: ...

    async def aclose(self) -> None: ...
