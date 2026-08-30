from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from datetime import timedelta
from enum import Enum
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import EmotionLabel, IntentType, RiskLevel
from app.models.entities import ChatMessage, ChatSession, UserAccount
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient, PromptTemplates, has_consult_signal, has_high_risk_signal
from app.services.assessment import PsychologicalAssessmentService, PsychologyAssessment
from app.services.cbt import DIMENSION_LABELS, CBTService, CBTState
from app.services.knowledge import KnowledgeService, SearchResult
from app.services.memory import RedisShortTermMemoryStore
from app.services.memory_cards import MemoryCardService
from app.services.risk_calibration import load_calibrated_risk_engine
from app.services.risk_trajectory import RiskTrajectoryHealth, RiskTrajectoryService
from app.services.user_profile import UserProfileService

logger = logging.getLogger(__name__)


def _json_value(value):
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump())
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


GENERAL_TASK_WORDS = [
    "java", "python", "javascript", "代码", "编程", "程序", "算法", "数据库", "spring", "maven",
    "前端", "后端", "项目", "接口", "bug", "报错", "作业", "论文", "翻译", "总结", "解释",
    "怎么写", "如何", "是什么", "为什么", "给我", "帮我", "推荐", "查询", "天气", "路线",
]


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
class AgentContext:
    user: UserAccount
    session: ChatSession
    original_input: str
    model_input: str
    memory_loaded: bool = False
    quick_safety_checked: bool = False
    quick_risk_flagged: bool = False
    intent_routed: bool = False
    knowledge_handled: bool = False
    risk_assessed: bool = False
    response_planned: bool = False
    finished: bool = False
    memory_brief: str = "无相关历史记忆。"
    intent: IntentType | None = None
    risk_level: RiskLevel = RiskLevel.LOW
    assessment: PsychologyAssessment | None = None
    knowledge_query: str = ""
    retrieved_knowledge: list[SearchResult] = field(default_factory=list)
    model_history: list[AiMessage] = field(default_factory=list)
    response_messages: list[AiMessage] = field(default_factory=list)
    response_agent: str = ""
    response_plan: str = ""
    steps: list[AgentStep] = field(default_factory=list)
    # Exam-anxiety closed-loop context (issues 03, 04, 07, 08, 09)
    exam_stage: str = ""
    memory_cards_context: str = ""
    cbt_active: bool = False
    cbt_complete: bool = False
    action_plan_created: bool = False
    trajectory_recorded: bool = False
    trajectory_trend: str = ""
    cbt_event: CbtEvent | None = None
    action_plan_event: ActionPlanEvent | None = None
    pending_review: bool = False

    def to_checkpoint(self) -> dict:
        payload = {
            key: _json_value(value)
            for key, value in vars(self).items()
            if key not in {"user", "session"}
        }
        payload["user"] = {
            "id": self.user.id,
            "username": self.user.username,
            "display_name": self.user.display_name,
            "roles_csv": self.user.roles_csv,
        }
        payload["session"] = {
            "id": self.session.id,
            "public_id": self.session.public_id,
            "user_id": self.session.user_id,
            "title": self.session.title,
            "no_memory": self.session.no_memory,
        }
        return payload

    @classmethod
    def from_checkpoint(
        cls,
        payload: dict,
        db: Session | None = None,
    ) -> AgentContext:
        user_data = payload["user"]
        session_data = payload["session"]
        user = db.get(UserAccount, user_data["id"]) if db is not None else None
        session = db.get(ChatSession, session_data["id"]) if db is not None else None
        user = user or UserAccount(**user_data)
        session = session or ChatSession(**session_data)

        values = {
            key: value
            for key, value in payload.items()
            if key not in {"user", "session"}
        }
        if values.get("intent"):
            values["intent"] = IntentType(values["intent"])
        values["risk_level"] = RiskLevel(values.get("risk_level", RiskLevel.LOW.value))
        if values.get("assessment"):
            assessment = values["assessment"]
            values["assessment"] = PsychologyAssessment(
                emotion=EmotionLabel(assessment["emotion"]),
                emotion_score=float(assessment["emotion_score"]),
                risk=RiskLevel(assessment["risk"]),
                confidence=float(assessment["confidence"]),
                summary=assessment["summary"],
                raw_risk_probabilities=assessment.get("raw_risk_probabilities", {}),
                risk_probabilities=assessment.get("risk_probabilities", {}),
                prediction_set=tuple(
                    RiskLevel(risk) for risk in assessment.get("prediction_set", [])
                ),
                uncertain=bool(assessment.get("uncertain", False)),
                requires_review=bool(assessment.get("requires_review", False)),
                model_version=assessment.get("model_version", ""),
                calibration_version=assessment.get("calibration_version", ""),
            )
        values["retrieved_knowledge"] = [
            SearchResult(**item) for item in values.get("retrieved_knowledge", [])
        ]
        values["model_history"] = [
            AiMessage(**item) for item in values.get("model_history", [])
        ]
        values["response_messages"] = [
            AiMessage(**item) for item in values.get("response_messages", [])
        ]
        values["steps"] = [AgentStep(**item) for item in values.get("steps", [])]
        if values.get("cbt_event"):
            values["cbt_event"] = CbtEvent(**values["cbt_event"])
        if values.get("action_plan_event"):
            action_plan = values["action_plan_event"]
            values["action_plan_event"] = ActionPlanEvent(
                plan_id=action_plan["plan_id"],
                items=[ActionPlanItemEvent(**item) for item in action_plan["items"]],
                feedback_due_at=action_plan.get("feedback_due_at"),
            )
        allowed = {item.name for item in fields(cls)} - {"user", "session"}
        return cls(
            user=user,
            session=session,
            **{key: value for key, value in values.items() if key in allowed},
        )


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
        return self.intent != IntentType.CHAT


@dataclass(frozen=True)
class AgentRuntimeDependencies:
    """Internal collaborators used by either runtime adapter."""

    ai: AiClient
    knowledge: KnowledgeService
    memory: RedisShortTermMemoryStore
    assessment: PsychologicalAssessmentService

    @classmethod
    def create(cls, db: Session, settings: Settings) -> AgentRuntimeDependencies:
        ai = AiClient(settings)
        calibrated_risk = None
        if settings.risk_calibration_artifact:
            artifact_path = Path(settings.risk_calibration_artifact)
            if not artifact_path.is_absolute():
                artifact_path = settings.project_root / artifact_path
            try:
                calibrated_risk = load_calibrated_risk_engine(artifact_path)
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                if settings.risk_calibration_required:
                    raise
                logger.warning("风险校准产物不可用，继续使用当前安全风险评估")
        return cls(
            ai=ai,
            knowledge=KnowledgeService(db, settings),
            memory=RedisShortTermMemoryStore(settings),
            assessment=PsychologicalAssessmentService(
                ai,
                calibrated_risk=calibrated_risk,
            ),
        )


class AgentRuntimeService:
    max_steps = 8

    def __init__(
        self,
        db: Session,
        settings: Settings,
        dependencies: AgentRuntimeDependencies | None = None,
    ):
        self.db = db
        self.settings = settings
        dependencies = dependencies or AgentRuntimeDependencies.create(db, settings)
        self.ai = dependencies.ai
        self.knowledge = dependencies.knowledge
        self.memory = dependencies.memory
        self.assessment = dependencies.assessment

    async def run(self, user: UserAccount, session: ChatSession, original_input: str, model_input: str) -> AgentRunResult:
        context = AgentContext(user=user, session=session, original_input=original_input, model_input=model_input)
        agents = [
            self.quick_safety_agent,
            self.memory_agent,
            self.supervisor_agent,
            self.risk_guardian_agent,
            self.knowledge_agent,
            self.cbt_agent,
            self.companion_agent,
            self.counselor_agent,
        ]
        for step in range(1, self.max_steps + 1):
            if context.finished:
                break
            for agent in agents:
                if await agent(step, context):
                    if context.risk_assessed and context.risk_level == RiskLevel.HIGH:
                        context.pending_review = True
                        context.finished = True
                        context.response_messages = []
                    break
        return AgentRunResult(
            intent=context.intent or IntentType.CHAT,
            risk_level=context.risk_level,
            assessment=context.assessment,
            retrieved_knowledge=context.retrieved_knowledge,
            response_messages=context.response_messages,
            steps=context.steps,
            trajectory_rising=context.trajectory_recorded,
            trajectory_trend=context.trajectory_trend,
            cbt_event=context.cbt_event,
            action_plan_event=context.action_plan_event,
            quick_safety_checked=context.quick_safety_checked,
            quick_risk_flagged=context.quick_risk_flagged,
            pending_review=context.pending_review,
        )

    async def memory_agent(self, step: int, context: AgentContext) -> bool:
        if context.memory_loaded:
            return False
        history = self.memory.load_recent(context.session.public_id)
        source = "redis"
        if not history:
            rows = (
                self.db.query(ChatMessage)
                .filter(ChatMessage.session_id == context.session.id)
                .order_by(ChatMessage.created_at.desc())
                .limit(self.settings.redis_memory_max_messages)
                .all()
            )
            rows.reverse()
            history = self.memory.messages_from_rows(rows)
            if history:
                self.memory.replace(context.session.public_id, history)
                source = "mysql_seeded"
        context.model_history = (history + [AiMessage(role="user", content=context.model_input)])[-self.settings.chat_history_limit * 2:]
        context.memory_brief = await self._summarize_memory(history, context.model_input)
        # A no-memory session retains same-session continuity but never reads
        # or changes the cross-session profile and confirmed memory cards.
        if not context.session.no_memory:
            # Load exam stage from user profile (issue 03)
            try:
                profile_svc = UserProfileService(self.db)
                context.exam_stage = profile_svc.get_stage_context(context.user.id)
            except Exception:
                pass
            # Load confirmed memory cards (issue 04)
            try:
                context.memory_cards_context = MemoryCardService(self.db).get_confirmed_context(context.user.id)
            except Exception:
                pass
        context.memory_loaded = True
        context.steps.append(AgentStep(step, "MemoryAgent", "READ_MEMORY", f"loaded {len(history)} messages from {source}; stage={context.exam_stage or 'none'}"))
        return True

    async def quick_safety_agent(self, step: int, context: AgentContext) -> bool:
        """Run the cheap explicit-signal guard before any memory I/O."""
        if context.quick_safety_checked:
            return False
        context.quick_risk_flagged = has_high_risk_signal(context.model_input)
        context.quick_safety_checked = True
        context.steps.append(AgentStep(
            step, "SafetyGuard", "QUICK_SAFETY_CHECK",
            "explicit high-risk signal detected" if context.quick_risk_flagged else "no explicit high-risk signal",
        ))
        return True

    async def supervisor_agent(self, step: int, context: AgentContext) -> bool:
        if not context.memory_loaded or context.intent_routed:
            return False
        context.intent = await self._classify(context.model_input, context.model_history)
        context.intent_routed = True
        if context.intent == IntentType.CHAT:
            context.knowledge_handled = True
        context.steps.append(AgentStep(step, "SupervisorAgent", "ROUTE_INTENT", f"intent={context.intent.value}"))
        return True

    async def knowledge_agent(self, step: int, context: AgentContext) -> bool:
        if (
            not context.intent_routed
            or context.knowledge_handled
            or context.intent == IntentType.CHAT
            or not context.risk_assessed
            or context.risk_level == RiskLevel.HIGH
        ):
            return False
        query = await self._rewrite_query(context)
        async_retrieve = getattr(self.knowledge, "aretrieve", None)
        if async_retrieve is None:
            retrieved = self.knowledge.retrieve(query, self.settings.knowledge_top_k)
        else:
            retrieved = await async_retrieve(query, self.settings.knowledge_top_k)
        context.knowledge_query = query
        context.retrieved_knowledge = retrieved
        context.knowledge_handled = True
        context.steps.append(AgentStep(step, "KnowledgeAgent", "RETRIEVE_KNOWLEDGE", f"query={query}; retrieved={len(retrieved)}"))
        return True

    async def risk_guardian_agent(self, step: int, context: AgentContext) -> bool:
        if not context.intent_routed or context.risk_assessed or context.intent == IntentType.CHAT:
            return False
        assessment = await self.assessment.aassess(context.model_input, context.model_history)
        context.assessment = assessment
        context.risk_level = assessment.risk
        context.risk_assessed = True
        # Record risk trajectory point and compute effective risk (issue 07)
        try:
            traj_svc = RiskTrajectoryService(
                self.db,
                session_window=self.settings.risk_trajectory_session_window,
                cross_session_days=self.settings.risk_trajectory_cross_session_days,
                rising_threshold=self.settings.risk_trajectory_rising_threshold,
            )
            effective_risk = traj_svc.get_effective_risk(
                context.user.id, context.session.id, assessment.risk, assessment.emotion_score
            )
            if effective_risk != assessment.risk:
                context.risk_level = effective_risk
                context.trajectory_recorded = True
                trend = traj_svc.get_trajectory_summary(context.user.id)
                context.trajectory_trend = (
                    f"连续上升（近 {self.settings.risk_trajectory_cross_session_days} 天 "
                    f"{trend.get('totalPoints', 0)} 个记录点，最新风险 {trend.get('currentRisk') or '未知'}）"
                )
            RiskTrajectoryHealth.record_success()
        except Exception as exc:
            try:
                self.db.rollback()
            except Exception:
                pass
            transitioned = RiskTrajectoryHealth.record_failure(exc)
            log = logger.warning if transitioned else logger.debug
            log("Risk trajectory evaluation degraded (%s)", type(exc).__name__)
        context.steps.append(AgentStep(step, "RiskGuardianAgent", "ASSESS_RISK", f"risk={assessment.risk.value}, emotion={assessment.emotion.value}; effective={context.risk_level.value}"))
        return True

    async def companion_agent(self, step: int, context: AgentContext) -> bool:
        if not context.intent_routed or context.intent != IntentType.CHAT or context.response_planned:
            return False
        context.risk_level = RiskLevel.LOW
        context.response_agent = "CompanionAgent"
        context.response_plan = "围绕用户当前问题直接、自然地回答。"
        context.response_messages = [
            PromptTemplates.answer_system_prompt(IntentType.CHAT, RiskLevel.LOW, "", context.user.display_name),
            AiMessage(role="system", content=f"当前由 CompanionAgent 负责回复。\n记忆摘要：\n{context.memory_brief}\n回复策略：\n{context.response_plan}"),
            *context.model_history,
        ]
        context.response_planned = True
        context.finished = True
        context.steps.append(AgentStep(step, "CompanionAgent", "PLAN_RESPONSE", "normal companion response planned"))
        return True

    async def counselor_agent(self, step: int, context: AgentContext) -> bool:
        if not context.risk_assessed or context.intent == IntentType.CHAT or context.response_planned:
            return False
        context.response_agent = "CounselorAgent"
        context.response_plan = "先共情，再给出具体支持步骤；高风险时优先安全。"
        knowledge_context = "\n\n".join(f"- [{item.source}] {item.content}" for item in context.retrieved_knowledge)
        context.response_messages = [
            PromptTemplates.answer_system_prompt(context.intent or IntentType.CONSULT, context.risk_level, knowledge_context, context.user.display_name),
            AiMessage(role="system", content=(
                f"当前由 CounselorAgent 负责回复。\n记忆摘要：\n{context.memory_brief}\n"
                f"KnowledgeAgent 检索 query：\n{context.knowledge_query}\n回复策略：\n{context.response_plan}"
            )),
            *context.model_history,
        ]
        context.response_planned = True
        context.finished = True
        context.steps.append(AgentStep(step, "CounselorAgent", "PLAN_RESPONSE", f"support response planned with risk={context.risk_level.value}"))
        return True


    async def cbt_agent(self, step: int, context: AgentContext) -> bool:
        """Four-part cognitive-behavioral questioning for non-high-risk support.

        Extracts four aspects from student input and asks about one missing aspect at a time.
        When all 4 dimensions are complete, generates a 24h action plan (issue 09).
        """
        if not context.risk_assessed or context.intent == IntentType.CHAT or context.response_planned:
            return False
        if context.risk_level == RiskLevel.HIGH:
            return False

        cbt_svc = CBTService(self.ai)
        saved_state = self.memory.load_cbt_state(context.session.public_id)
        current_state = CBTState.from_dict(saved_state) if saved_state else CBTState()
        current_state = cbt_svc.extract_dimensions(context.model_input, current_state)

        if current_state.is_complete:
            context.cbt_complete = True
            context.cbt_active = False
            self.memory.save_cbt_state(context.session.public_id, current_state.to_dict())
            try:
                from app.services.action_plan import ActionPlanService
                cbt_summary = (
                    f"触发事件: {current_state.trigger_event}; "
                    f"想法: {current_state.thoughts}; "
                    f"身体反应: {current_state.body_reactions}; "
                    f"行为: {current_state.behavior}"
                )
                plan_svc = ActionPlanService(self.db, self.ai)
                plan = plan_svc.generate_plan(
                    context.user.id,
                    context.session.id,
                    cbt_summary,
                    context.exam_stage,
                    commit=False,
                )
                context.action_plan_created = True
                context.cbt_event = CbtEvent(
                    active=False,
                    completed_count=current_state.completed_count,
                    next_dimension=None,
                    complete=True,
                )
                context.action_plan_event = ActionPlanEvent(
                    plan_id=plan.id,
                    feedback_due_at=(plan.created_at + timedelta(hours=plan.target_window_hours)).isoformat(),
                    items=[
                        ActionPlanItemEvent(
                            id=item.id,
                            content=item.content,
                            order=item.order_index,
                            completed=item.completed,
                        )
                        for item in sorted(plan.items, key=lambda x: x.order_index)
                    ],
                )
                context.response_agent = "CounselorAgent"
                context.response_plan = "four-part questioning complete; action plan generated"
                knowledge_context = "\n\n".join(
                    f"- [{item.source}] {item.content}" for item in context.retrieved_knowledge
                )
                plan_items_text = "\n".join(
                    f"{i + 1}. {item.content}"
                    for i, item in enumerate(sorted(plan.items, key=lambda x: x.order_index))
                )
                context.response_messages = [
                    PromptTemplates.answer_system_prompt(
                        context.intent or IntentType.CONSULT,
                        context.risk_level, knowledge_context, context.user.display_name,
                    ),
                    AiMessage(role="system", content=(
                        "CounselorAgent responding. All four aspects are complete.\n"
                        f"Memory brief: {context.memory_brief}\n"
                        f"Action plan generated ({len(plan.items)} items). "
                        "Summarize the four-part findings and introduce the plan.\n"
                        f"以下是系统已生成的行动计划条目，请在回复中逐一介绍，确保内容与这些条目完全一致：\n{plan_items_text}\n"
                        f"Strategy: {context.response_plan}"
                    )),
                    *context.model_history,
                ]
                context.response_planned = True
                context.finished = True
                context.steps.append(AgentStep(
                    step, "CBTAgent", "GENERATE_ACTION_PLAN",
                    f"4 dimensions complete; plan={plan.id}",
                ))
            except Exception:
                return False
            return True
        else:
            context.cbt_active = True
            self.memory.save_cbt_state(context.session.public_id, current_state.to_dict())
            next_q = cbt_svc.get_next_question(current_state, context.exam_stage)
            next_dimension = current_state.next_dimension or ""
            context.response_agent = "CBTAgent"
            context.response_plan = "four-part cognitive-behavioral questioning"
            knowledge_context = "\n\n".join(f"- [{item.source}] {item.content}" for item in context.retrieved_knowledge)
            context.response_messages = [
                PromptTemplates.answer_system_prompt(context.intent or IntentType.CONSULT, context.risk_level, knowledge_context, context.user.display_name),
                AiMessage(role="system", content=(
                    f"当前由 CBTAgent 负责回复。\n记忆摘要：\n{context.memory_brief}\n"
                    f"认知行为四维追问策略：需要了解学生的「{DIMENSION_LABELS.get(next_dimension, next_dimension)}」方面。\n"
                    f"参考问题：{next_q}\n"
                    "请以共情、自然的方式引导学生回答这个问题，不要直接暴露四个方面的内部名称，也不要机械提问。"
                )),
                *context.model_history,
            ]
            context.response_planned = True
            context.finished = True
            completed = current_state.completed_count
            context.cbt_event = CbtEvent(
                active=True,
                completed_count=completed,
                next_dimension=current_state.next_dimension,
                complete=False,
            )
            context.steps.append(AgentStep(
                step, "CBTAgent", "ASK_CBT_QUESTION",
                f"completed={completed}/4; next={current_state.next_dimension}",
            ))
            return True

    async def _classify(self, text: str, history: list[AiMessage]) -> IntentType:
        lowered = text.lower()
        if has_high_risk_signal(lowered):
            return IntentType.RISK
        if not has_consult_signal(lowered) and any(word in lowered for word in GENERAL_TASK_WORDS):
            return IntentType.CHAT
        try:
            label = (await self.ai.acomplete(PromptTemplates.intent_prompt(history, text))).upper()
            if "RISK" in label:
                return IntentType.RISK
            if "CONSULT" in label:
                return IntentType.CONSULT
            if "CHAT" in label:
                return IntentType.CHAT
        except Exception:
            pass
        return IntentType.CONSULT if has_consult_signal(lowered) else IntentType.CHAT

    async def _rewrite_query(self, context: AgentContext) -> str:
        try:
            query = (await self.ai.acomplete([
                AiMessage(role="system", content="你是 Xling 的 KnowledgeAgent。把学生输入改写成适合检索校园心理知识库的中文查询词，只输出查询词。"),
                AiMessage(role="user", content=f"记忆摘要：\n{context.memory_brief}\n\n当前输入：\n{context.model_input}"),
            ])).strip()
            return (query or context.model_input)[:60]
        except Exception:
            return context.model_input

    async def _summarize_memory(self, history: list[AiMessage], current_input: str) -> str:
        if not history:
            return "无相关历史记忆。"
        try:
            summary = (await self.ai.acomplete([
                AiMessage(role="system", content="你是 Xling 的 MemoryAgent。只输出 1-3 条中文记忆要点，不输出风险等级或诊断。"),
                AiMessage(role="user", content=f"当前输入：\n{current_input}\n\n最近历史：\n{history[-12:]}"),
            ])).strip()
            return summary[:400] or "无相关历史记忆。"
        except Exception:
            return "无相关历史记忆。"
