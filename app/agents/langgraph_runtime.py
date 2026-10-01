from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any, Awaitable, Callable, TypedDict
from uuid import uuid4

from sqlalchemy.orm import Session

from app.agents.runtime import (
    ActionPlanEvent,
    ActionPlanItemEvent,
    AgentRunResult,
    AgentRuntimeDependencies,
    AgentStep,
    CbtEvent,
)
from app.core import diagnostics
from app.core.config import Settings
from app.core.enums import EmotionLabel, IntentType, RiskLevel
from app.core.time import utc_now
from app.models.entities import ChatMessage, ChatSession, UserAccount
from app.schemas.dtos import AiMessage
from app.services.ai import PromptTemplates, has_consult_signal, has_high_risk_signal
from app.services.assessment import PsychologyAssessment
from app.services.cbt import DIMENSION_LABELS, CBTService, CBTState
from app.services.knowledge import SearchResult
from app.services.memory_cards import MemoryCardService
from app.services.risk_trajectory import RiskTrajectoryHealth, RiskTrajectoryService
from app.services.user_profile import UserProfileService

GENERAL_TASK_WORDS = [
    "java", "python", "javascript", "代码", "编程", "程序", "算法", "数据库", "spring", "maven",
    "前端", "后端", "项目", "接口", "bug", "报错", "作业", "论文", "翻译", "总结", "解释",
    "怎么写", "如何", "是什么", "为什么", "给我", "帮我", "推荐", "查询", "天气", "路线",
]


class GraphState(TypedDict):
    """Primitive checkpoint values for one conversation turn; nodes return field updates."""

    user_id: int
    session_id: int
    thread_id: str
    display_name: str
    no_memory: bool
    model_input: str
    original_run_id: str
    quick_risk_flagged: bool
    intent: str | None
    risk_level: str
    assessment: dict[str, Any] | None
    memory_brief: str
    support_background_context: str
    memory_cards_context: str
    model_history: list[dict[str, str]]
    knowledge_query: str
    retrieved_knowledge: list[dict[str, Any]]
    response_messages: list[dict[str, str]]
    steps: list[dict[str, Any]]
    trajectory_rising: bool
    trajectory_trend: str
    review_decision: str | None
    cbt_event: dict[str, Any] | None
    action_plan_event: dict[str, Any] | None


GraphUpdate = dict[str, Any]


@dataclass(frozen=True)
class RuntimeStateSnapshot:
    values: dict[str, Any]
    next: tuple[str, ...]


_SHARED_MEMORY_SAVER: Any = None


class LangGraphAgentRuntimeService:
    """The sole conversation orchestrator, with durable human-review interruption."""

    framework_name = "langgraph"

    def __init__(
        self, db: Session, settings: Settings, dependencies: AgentRuntimeDependencies | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        dependencies = dependencies or AgentRuntimeDependencies.create(db, settings)
        self.ai = dependencies.ai
        self.knowledge = dependencies.knowledge
        self.memory = dependencies.memory
        self.assessment = dependencies.assessment
        self._sqlite_conn: Any = None
        self._sqlite_conn_started = False
        self._checkpointer_ready = False
        self._checkpointer = self._make_checkpointer()
        self.graph = self._build_graph()

    async def run(
        self, user: UserAccount, session: ChatSession, original_input: str, model_input: str,
    ) -> AgentRunResult:
        """Initialize every turn field before entering the graph, including empty business events."""
        await self._ensure_checkpointer()
        state: GraphState = {
            "user_id": user.id, "session_id": session.id, "thread_id": session.public_id,
            "display_name": user.display_name, "no_memory": bool(session.no_memory),
            "model_input": model_input,
            "original_run_id": diagnostics.current_run_id() or str(uuid4()),
            "quick_risk_flagged": False, "intent": None, "risk_level": RiskLevel.LOW.value,
            "assessment": None, "memory_brief": "无相关历史记忆。",
            "support_background_context": "", "memory_cards_context": "", "model_history": [],
            "knowledge_query": "", "retrieved_knowledge": [], "response_messages": [], "steps": [],
            "trajectory_rising": False, "trajectory_trend": "", "review_decision": None,
            "cbt_event": None, "action_plan_event": None,
        }
        config = {"configurable": {"thread_id": session.public_id}}
        with diagnostics.stage("graph.run"):
            await self.graph.ainvoke(state, config=config)
            await self._record_checkpoint_activity(session.public_id)
            snapshot = await self.graph.aget_state(config)
        pending_review = bool(snapshot.next)
        diagnostics.emit("graph.interrupted" if pending_review else "graph.completed", requires_review=pending_review)
        return self._result(snapshot.values, pending_review=pending_review)

    def get_state(self, thread_id: str) -> RuntimeStateSnapshot:
        state = self.graph.get_state({"configurable": {"thread_id": thread_id}})
        return RuntimeStateSnapshot(dict(state.values), tuple(state.next))

    async def aget_state(self, thread_id: str) -> RuntimeStateSnapshot:
        await self._ensure_checkpointer()
        state = await self.graph.aget_state({"configurable": {"thread_id": thread_id}})
        return RuntimeStateSnapshot(dict(state.values), tuple(state.next))

    async def resume(self, thread_id: str, approved: bool) -> AgentRunResult:
        """Resume only a valid new-format interruption; unavailable state has a fixed safe result."""
        from langgraph.types import Command

        config = {"configurable": {"thread_id": thread_id}}
        try:
            with diagnostics.stage("checkpoint.read", thread_id=thread_id):
                await self._ensure_checkpointer()
                existing = await self.graph.aget_state(config)
            if (
                not GraphState.__required_keys__.issubset(existing.values)
                or "risk_guardian_gate" not in existing.next
                or existing.values.get("risk_level") != RiskLevel.HIGH.value
                or existing.values.get("intent") not in {IntentType.CONSULT.value, IntentType.RISK.value}
                or not existing.values.get("assessment")
            ):
                diagnostics.degraded("checkpoint.read", "checkpoint_unavailable")
                return self._degraded_result()
            # Validation is of this graph's state, never a conversion of retired checkpoints.
            saved = self._result(existing.values, pending_review=True)
            if saved.assessment is None or (
                saved.assessment.risk != RiskLevel.HIGH
                and not (saved.assessment.risk == RiskLevel.MEDIUM and saved.trajectory_rising)
            ):
                diagnostics.degraded("checkpoint.read", "checkpoint_unavailable")
                return self._degraded_result()
        except Exception as exc:
            diagnostics.degraded("checkpoint.read", "checkpoint_unavailable", exc)
            return self._degraded_result()
        diagnostics.bind(origin_run_id=existing.values["original_run_id"], thread_id=thread_id)
        with diagnostics.stage("graph.resume", review_action="approve" if approved else "reject"):
            await self.graph.ainvoke(Command(resume={"approved": approved}), config=config)
            await self._record_checkpoint_activity(thread_id)
            snapshot = await self.graph.aget_state(config)
        diagnostics.emit("graph.completed", requires_review=False)
        result = self._result(snapshot.values)
        if not approved:
            result.fallback_response = PromptTemplates.fallback_response()
            result.response_messages = []
        return result

    @staticmethod
    def _degraded_result() -> AgentRunResult:
        return AgentRunResult(
            intent=IntentType.RISK, risk_level=RiskLevel.HIGH, assessment=None,
            retrieved_knowledge=[], response_messages=[], steps=[],
            fallback_response=PromptTemplates.fallback_response(), degraded=True,
        )

    @staticmethod
    def _result(state: dict[str, Any], *, pending_review: bool = False) -> AgentRunResult:
        assessment = state["assessment"]
        plan = state["action_plan_event"]
        return AgentRunResult(
            intent=IntentType(state["intent"] or IntentType.CHAT.value),
            risk_level=RiskLevel(state["risk_level"]),
            assessment=PsychologyAssessment(
                emotion=EmotionLabel(assessment["emotion"]), emotion_score=assessment["emotion_score"],
                risk=RiskLevel(assessment["risk"]), confidence=assessment["confidence"],
                summary=assessment["summary"], model_version=assessment["model_version"],
            ) if assessment else None,
            retrieved_knowledge=[SearchResult(**item) for item in state["retrieved_knowledge"]],
            response_messages=[] if pending_review else [AiMessage(**item) for item in state["response_messages"]],
            steps=[AgentStep(**item) for item in state["steps"]], pending_review=pending_review,
            trajectory_rising=state["trajectory_rising"], trajectory_trend=state["trajectory_trend"],
            cbt_event=CbtEvent(**state["cbt_event"]) if state["cbt_event"] else None,
            action_plan_event=ActionPlanEvent(
                plan_id=plan["plan_id"], items=[ActionPlanItemEvent(**item) for item in plan["items"]],
                feedback_due_at=plan["feedback_due_at"],
            ) if plan else None,
            quick_safety_checked=True, quick_risk_flagged=state["quick_risk_flagged"],
        )

    def _make_checkpointer(self):
        """按配置创建执行状态保存器。

        文件数据库方式准备目录与异步连接对象；其他配置使用进程内共享保存器。
        进程内共享仅让同一进程的不同实例恢复状态，不能保证重启或跨进程恢复。
        """
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        serde = JsonPlusSerializer()
        backend = self.settings.langgraph_checkpoint_backend.lower()
        # 文件方式可在新实例中恢复；这里只建立连接对象，真正连接由异步准备函数完成。
        if backend in {"sqlite", "async_sqlite"}:
            import aiosqlite
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            path = self.settings.langgraph_checkpoint_path
            if not os.path.isabs(path):
                path = str(self.settings.project_root / path)
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            conn = aiosqlite.connect(path)
            self._sqlite_conn = conn
            return AsyncSqliteSaver(conn, serde=serde)
        from langgraph.checkpoint.memory import MemorySaver

        # 内存方式供同进程各请求共享，进程退出后内容不会保留。
        global _SHARED_MEMORY_SAVER
        if _SHARED_MEMORY_SAVER is None:
            _SHARED_MEMORY_SAVER = MemorySaver(serde=serde)
        return _SHARED_MEMORY_SAVER

    async def _ensure_checkpointer(self) -> None:
        """在首次使用时连接状态数据库并准备表结构及过期清理。

        准备完成后设置标志，后续调用跳过重复初始化；失败时不会标记为已就绪。
        """
        if self._checkpointer_ready:
            return
        if self._sqlite_conn is not None:
            await self._sqlite_conn
            self._sqlite_conn_started = True
            await self._checkpointer.setup()
            await self._setup_checkpoint_retention()
        self._checkpointer_ready = True

    async def aclose(self) -> None:
        """关闭当前实例已经启动的异步状态数据库连接。

        未启动连接时不做处理；关闭后清除就绪标志，避免仍把已关闭资源视为可用。
        """
        if self._sqlite_conn is not None and self._sqlite_conn_started:
            await self._sqlite_conn.close()
            self._checkpointer_ready = False
            self._sqlite_conn_started = False

    async def _setup_checkpoint_retention(self) -> None:
        """准备活动时间表并删除超过保留期限的已登记会话状态。

        保留期限至少为一天；按活动表找出过期会话，再删除写入记录、状态快照及活动记录并提交。
        """
        connection = self._sqlite_conn
        if connection is None:
            return
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS xling_checkpoint_activity (
                thread_id TEXT PRIMARY KEY,
                updated_at TEXT NOT NULL
            )
            """
        )
        cutoff = (
            utc_now() - timedelta(days=max(1, self.settings.langgraph_checkpoint_retention_days))
        ).isoformat()
        cursor = await connection.execute(
            "SELECT thread_id FROM xling_checkpoint_activity WHERE updated_at < ?",
            (cutoff,),
        )
        # 只清理活动表中已超过期限的会话，按编号同时移除快照和待写记录。
        expired = [row[0] for row in await cursor.fetchall()]
        for thread_id in expired:
            await connection.execute("DELETE FROM writes WHERE thread_id = ?", (thread_id,))
            await connection.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,))
            await connection.execute(
                "DELETE FROM xling_checkpoint_activity WHERE thread_id = ?",
                (thread_id,),
            )
        await connection.commit()

    async def _record_checkpoint_activity(self, thread_id: str) -> None:
        """记录指定会话最近一次执行或恢复的时间。

        文件数据库方式插入或更新同一编号的记录并提交；进程内存储方式直接返回。
        """
        if self._sqlite_conn is None:
            return
        connection = self._sqlite_conn
        await connection.execute(
            """
            INSERT INTO xling_checkpoint_activity(thread_id, updated_at)
            VALUES (?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET updated_at = excluded.updated_at
            """,
            (thread_id, utc_now().isoformat()),
        )
        await connection.commit()

    def _build_graph(self):
        from langgraph.graph import END, StateGraph

        graph = StateGraph(GraphState)
        nodes = {
            "quick_safety": self._quick_safety_node,
            "memory": self._memory_node,
            "supervisor": self._supervisor_node,
            "risk_guardian": self._risk_guardian_node,
            "risk_guardian_gate": self._risk_guardian_gate,
            "knowledge": self._knowledge_node,
            "cbt": self._cbt_node,
            "companion": self._companion_node,
            "counselor": self._counselor_node,
        }
        for name, node in nodes.items():
            graph.add_node(name, self._observed_node(name, node))
        graph.set_entry_point("quick_safety")
        graph.add_edge("quick_safety", "memory")
        graph.add_edge("memory", "supervisor")
        graph.add_conditional_edges(
            "supervisor", self._route_after_supervisor, {"chat": "companion", "support": "risk_guardian"},
        )
        graph.add_edge("risk_guardian", "risk_guardian_gate")
        graph.add_conditional_edges(
            "risk_guardian_gate", self._route_after_gate,
            {"approved": "counselor", "rejected": END, "support": "knowledge"},
        )
        graph.add_edge("knowledge", "cbt")
        graph.add_conditional_edges("cbt", self._route_after_cbt, {"handled": END, "skip": "counselor"})
        graph.add_edge("companion", END)
        graph.add_edge("counselor", END)
        return graph.compile(checkpointer=self._checkpointer)

    @staticmethod
    def _observed_node(
        name: str, node: Callable[[GraphState], Awaitable[GraphUpdate]],
    ) -> Callable[[GraphState], Awaitable[GraphUpdate]]:
        async def observed(state: GraphState) -> GraphUpdate:
            with diagnostics.stage(f"node.{name}"):
                return await node(state)
        return observed

    @staticmethod
    def _steps(state: GraphState, agent: str, action: str, observation: str) -> list[dict[str, Any]]:
        return [*state["steps"], asdict(AgentStep(len(state["steps"]) + 1, agent, action, observation))]

    @staticmethod
    def _history(state: GraphState) -> list[AiMessage]:
        return [AiMessage(**item) for item in state["model_history"]]

    @staticmethod
    def _messages(messages: list[AiMessage]) -> list[dict[str, str]]:
        return [{"role": item.role, "content": item.content} for item in messages]

    async def _quick_safety_node(self, state: GraphState) -> GraphUpdate:
        flagged = has_high_risk_signal(state["model_input"])
        diagnostics.emit("safety.checked", quick_risk_flagged=flagged)
        return {
            "quick_risk_flagged": flagged,
            "steps": self._steps(state, "SafetyGuard", "QUICK_SAFETY_CHECK",
                                 "explicit high-risk signal detected" if flagged else "no explicit high-risk signal"),
        }

    async def _memory_node(self, state: GraphState) -> GraphUpdate:
        history = self.memory.load_recent(state["thread_id"])
        source = "redis"
        if not history:
            rows = (
                self.db.query(ChatMessage).filter(ChatMessage.session_id == state["session_id"])
                .order_by(ChatMessage.created_at.desc()).limit(self.settings.redis_memory_max_messages).all()
            )
            rows.reverse()
            history = self.memory.messages_from_rows(rows)
            if history:
                self.memory.replace(state["thread_id"], history)
                source = "mysql_seeded"
        model_history = (
            history + [AiMessage(role="user", content=state["model_input"])]
        )[-self.settings.chat_history_limit * 2:]
        brief = await self._summarize_memory(history, state["model_input"])
        support_background = ""
        cards = ""
        if not state["no_memory"]:
            try:
                with diagnostics.stage("memory.support_background"):
                    support_background = UserProfileService(self.db).get_support_context(state["user_id"])
            except Exception as exc:
                diagnostics.degraded("memory.support_background", "support_background_unavailable", exc)
            try:
                with diagnostics.stage("memory.confirmed_cards"):
                    cards = MemoryCardService(self.db).get_confirmed_context(state["user_id"])
            except Exception as exc:
                diagnostics.degraded("memory.confirmed_cards", "confirmed_cards_unavailable", exc)
        return {
            "model_history": self._messages(model_history), "memory_brief": brief,
            "support_background_context": support_background, "memory_cards_context": cards,
            "steps": self._steps(state, "MemoryAgent", "READ_MEMORY", f"loaded {len(history)} messages from {source}"),
        }

    async def _supervisor_node(self, state: GraphState) -> GraphUpdate:
        intent = await self._classify(state["model_input"], self._history(state))
        return {
            "intent": intent.value,
            "steps": self._steps(state, "SupervisorAgent", "ROUTE_INTENT", f"intent={intent.value}"),
        }

    def _route_after_supervisor(self, state: GraphState) -> str:
        route = "chat" if state["intent"] == IntentType.CHAT.value else "support"
        diagnostics.emit("branch.selected", route=route, intent=state["intent"],
                         quick_risk_flagged=state["quick_risk_flagged"])
        return route

    async def _risk_guardian_node(self, state: GraphState) -> GraphUpdate:
        assessment = await self.assessment.aassess(state["model_input"], self._history(state))
        risk = assessment.risk
        rising = False
        trend = ""
        try:
            with diagnostics.stage("risk.trajectory"):
                service = RiskTrajectoryService(
                    self.db, session_window=self.settings.risk_trajectory_session_window,
                    cross_session_days=self.settings.risk_trajectory_cross_session_days,
                    rising_threshold=self.settings.risk_trajectory_rising_threshold,
                )
                risk = service.get_effective_risk(
                    state["user_id"], state["session_id"], assessment.risk, assessment.emotion_score,
                )
                if risk != assessment.risk:
                    rising = True
                    summary = service.get_trajectory_summary(state["user_id"])
                    trend = (
                        f"连续上升（近 {self.settings.risk_trajectory_cross_session_days} 天 "
                        f"{summary.get('totalPoints', 0)} 个记录点，最新风险 {summary.get('currentRisk') or '未知'}）"
                    )
                RiskTrajectoryHealth.record_success()
        except Exception as exc:
            try:
                self.db.rollback()
            except Exception:
                pass
            RiskTrajectoryHealth.record_failure(exc)
            diagnostics.degraded("risk.trajectory", "risk_trajectory_unavailable", exc)
        return {
            "assessment": {
                "emotion": assessment.emotion.value, "emotion_score": assessment.emotion_score,
                "risk": assessment.risk.value, "confidence": assessment.confidence,
                "summary": assessment.summary, "model_version": assessment.model_version,
            },
            "risk_level": risk.value, "trajectory_rising": rising, "trajectory_trend": trend,
            "steps": self._steps(state, "RiskGuardianAgent", "ASSESS_RISK",
                                 f"risk={assessment.risk.value}, emotion={assessment.emotion.value}; effective={risk.value}"),
        }

    async def _risk_guardian_gate(self, state: GraphState) -> GraphUpdate:
        if state["risk_level"] != RiskLevel.HIGH.value:
            return {"review_decision": None}
        from langgraph.types import interrupt

        decision = interrupt({"risk": "HIGH", "summary": (state["assessment"] or {}).get("summary", "")})
        return {"review_decision": "approved" if decision["approved"] else "rejected"}

    def _route_after_gate(self, state: GraphState) -> str:
        route = state["review_decision"] or "support"
        diagnostics.emit("branch.selected", route=route, risk_level=state["risk_level"],
                         requires_review=state["risk_level"] == RiskLevel.HIGH.value)
        return route

    async def _knowledge_node(self, state: GraphState) -> GraphUpdate:
        query = await self._rewrite_query(state["memory_brief"], state["model_input"])
        with diagnostics.stage("knowledge.retrieve"):
            async_retrieve = getattr(self.knowledge, "aretrieve", None)
            results = (self.knowledge.retrieve(query, self.settings.knowledge_top_k) if async_retrieve is None
                       else await async_retrieve(query, self.settings.knowledge_top_k))
        diagnostics.emit("knowledge.retrieved", retrieval_count=len(results))
        return {
            "knowledge_query": query, "retrieved_knowledge": [asdict(item) for item in results],
            "steps": self._steps(state, "KnowledgeAgent", "RETRIEVE_KNOWLEDGE", f"retrieved={len(results)}"),
        }

    async def _companion_node(self, state: GraphState) -> GraphUpdate:
        messages = [
            PromptTemplates.answer_system_prompt(IntentType.CHAT, RiskLevel.LOW, "", state["display_name"]),
            AiMessage(role="system", content=(
                f"当前由 CompanionAgent 负责回复。\n记忆摘要：\n{state['memory_brief']}\n"
                f"支持背景：\n{state['support_background_context'] or '无'}\n"
                f"已确认记忆：\n{state['memory_cards_context'] or '无'}\n"
                "回复策略：\n围绕用户当前问题直接、自然地回答。"
            )),
            *self._history(state),
        ]
        return {
            "response_messages": self._messages(messages),
            "steps": self._steps(state, "CompanionAgent", "PLAN_RESPONSE", "normal companion response planned"),
        }

    @staticmethod
    def _knowledge_context(state: GraphState) -> str:
        return "\n\n".join(f"- [{item['source']}] {item['content']}" for item in state["retrieved_knowledge"])

    async def _counselor_node(self, state: GraphState) -> GraphUpdate:
        messages = [
            PromptTemplates.answer_system_prompt(
                IntentType(state["intent"] or IntentType.CONSULT.value), RiskLevel(state["risk_level"]),
                self._knowledge_context(state), state["display_name"],
            ),
            AiMessage(role="system", content=(
                f"当前由 CounselorAgent 负责回复。\n记忆摘要：\n{state['memory_brief']}\n"
                f"支持背景：\n{state['support_background_context'] or '无'}\n"
                f"已确认记忆：\n{state['memory_cards_context'] or '无'}\n"
                f"KnowledgeAgent 检索 query：\n{state['knowledge_query']}\n"
                "回复策略：\n先共情，再给出具体支持步骤；高风险时优先安全。"
            )),
            *self._history(state),
        ]
        return {
            "response_messages": self._messages(messages),
            "steps": self._steps(state, "CounselorAgent", "PLAN_RESPONSE",
                                 f"support response planned with risk={state['risk_level']}"),
        }

    async def _cbt_node(self, state: GraphState) -> GraphUpdate:
        service = CBTService(self.ai)
        saved = self.memory.load_cbt_state(state["thread_id"])
        previous = CBTState.from_dict(saved) if saved else CBTState()
        with diagnostics.stage("support.extract_dimensions"):
            current = service.extract_dimensions(state["model_input"], previous)
        self.memory.save_cbt_state(state["thread_id"], current.to_dict())
        system_prompt = PromptTemplates.answer_system_prompt(
            IntentType(state["intent"] or IntentType.CONSULT.value), RiskLevel(state["risk_level"]),
            self._knowledge_context(state), state["display_name"],
        )
        if current.is_complete:
            from app.services.action_plan import ActionPlanService

            summary = (f"触发事件: {current.trigger_event}; 想法: {current.thoughts}; "
                       f"身体反应: {current.body_reactions}; 行为: {current.behavior}")
            try:
                with diagnostics.stage("support.create_action_plan"):
                    # Keep a failed plan write from poisoning the surrounding turn transaction.
                    with self.db.begin_nested():
                        plan = ActionPlanService(self.db, self.ai).generate_plan(
                            state["user_id"], state["session_id"], summary, commit=False,
                        )
                    items = sorted(plan.items, key=lambda item: item.order_index)
                    event = ActionPlanEvent(
                        plan.id,
                        [ActionPlanItemEvent(item.id, item.content, item.order_index, item.completed) for item in items],
                        (plan.created_at + timedelta(hours=plan.target_window_hours)).isoformat(),
                    )
                    items_text = "\n".join(f"{index + 1}. {item.content}" for index, item in enumerate(items))
            except Exception as exc:
                diagnostics.degraded("support.create_action_plan", "action_plan_unavailable", exc)
                return {"cbt_event": None}
            instruction = AiMessage(role="system", content=(
                "CounselorAgent responding. All four aspects are complete.\n"
                f"Memory brief: {state['memory_brief']}\n"
                f"Support background: {state['support_background_context'] or '无'}\n"
                f"Confirmed memory: {state['memory_cards_context'] or '无'}\n"
                f"Action plan generated ({len(items)} items). "
                "Summarize the four-part findings and introduce the plan.\n"
                f"以下是系统已生成的行动计划条目，请在回复中逐一介绍，确保内容与这些条目完全一致：\n{items_text}\n"
                "Strategy: four-part questioning complete; action plan generated"
            ))
            return {
                "cbt_event": asdict(CbtEvent(False, current.completed_count, None, True)),
                "action_plan_event": asdict(event),
                "response_messages": self._messages([system_prompt, instruction, *self._history(state)]),
                "steps": self._steps(state, "CBTAgent", "GENERATE_ACTION_PLAN", f"4 dimensions complete; plan={plan.id}"),
            }
        next_dimension = current.next_dimension or ""
        instruction = AiMessage(role="system", content=(
            f"当前由 CBTAgent 负责回复。\n记忆摘要：\n{state['memory_brief']}\n"
            f"支持背景：\n{state['support_background_context'] or '无'}\n"
            f"已确认记忆：\n{state['memory_cards_context'] or '无'}\n"
            f"认知行为四维追问策略：需要了解用户的「{DIMENSION_LABELS.get(next_dimension, next_dimension)}」方面。\n"
            f"参考问题：{service.get_next_question(current)}\n"
            "请以共情、自然的方式引导用户回答这个问题，不要直接暴露四个方面的内部名称，也不要机械提问。"
        ))
        return {
            "cbt_event": asdict(CbtEvent(True, current.completed_count, current.next_dimension, False)),
            "response_messages": self._messages([system_prompt, instruction, *self._history(state)]),
            "steps": self._steps(state, "CBTAgent", "ASK_CBT_QUESTION",
                                 f"completed={current.completed_count}/4; next={current.next_dimension}"),
        }

    def _route_after_cbt(self, state: GraphState) -> str:
        route = "handled" if state["cbt_event"] is not None else "skip"
        diagnostics.emit("branch.selected", route=route,
                         interview_completed=bool(state["cbt_event"] and state["cbt_event"]["complete"]))
        return route

    async def _classify(self, text: str, history: list[AiMessage]) -> IntentType:
        lowered = text.lower()
        if has_high_risk_signal(lowered):
            diagnostics.emit("classification.completed", intent=IntentType.RISK.value, reason_code="explicit_risk_signal")
            return IntentType.RISK
        if not has_consult_signal(lowered) and any(word in lowered for word in GENERAL_TASK_WORDS):
            diagnostics.emit("classification.completed", intent=IntentType.CHAT.value, reason_code="ordinary_task_signal")
            return IntentType.CHAT
        try:
            with diagnostics.stage("model.classify"):
                label = (await self.ai.acomplete(PromptTemplates.intent_prompt(history, text))).upper()
            for intent in (IntentType.RISK, IntentType.CONSULT, IntentType.CHAT):
                if intent.value in label:
                    diagnostics.emit("classification.completed", intent=intent.value, reason_code="model_label")
                    return intent
            diagnostics.degraded("model.classify", "unrecognized_intent_label")
        except Exception as exc:
            diagnostics.degraded("model.classify", "intent_model_unavailable", exc)
        return IntentType.CONSULT if has_consult_signal(lowered) else IntentType.CHAT

    async def _rewrite_query(self, memory_brief: str, model_input: str) -> str:
        try:
            with diagnostics.stage("model.rewrite_query"):
                query = (await self.ai.acomplete([
                    AiMessage(role="system", content="你是 Xling 的 KnowledgeAgent。把用户输入改写成适合检索通用心理健康支持知识库的中文查询词，只输出查询词。"),
                    AiMessage(role="user", content=f"记忆摘要：\n{memory_brief}\n\n当前输入：\n{model_input}"),
                ])).strip()
            if not query:
                diagnostics.degraded("model.rewrite_query", "empty_query")
            return (query or model_input)[:60]
        except Exception as exc:
            diagnostics.degraded("model.rewrite_query", "query_model_unavailable", exc)
            return model_input

    async def _summarize_memory(self, history: list[AiMessage], current_input: str) -> str:
        if not history:
            return "无相关历史记忆。"
        try:
            with diagnostics.stage("model.summarize_memory"):
                summary = (await self.ai.acomplete([
                    AiMessage(role="system", content="你是 Xling 的 MemoryAgent。只输出 1-3 条中文记忆要点，不输出风险等级或诊断。"),
                    AiMessage(role="user", content=f"当前输入：\n{current_input}\n\n最近历史：\n{history[-12:]}"),
                ])).strip()
            if not summary:
                diagnostics.degraded("model.summarize_memory", "empty_memory_summary")
            return summary[:400] or "无相关历史记忆。"
        except Exception as exc:
            diagnostics.degraded("model.summarize_memory", "memory_model_unavailable", exc)
            return "无相关历史记忆。"
