from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, TypedDict

from sqlalchemy.orm import Session

from app.agents.runtime import (
    AgentContext,
    AgentRunResult,
    AgentRuntimeDependencies,
    AgentRuntimeService,
)
from app.core.config import Settings
from app.core.enums import IntentType, RiskLevel
from app.core.time import utc_now
from app.models.entities import ChatSession, UserAccount


class GraphState(TypedDict):
    context: dict


@dataclass(frozen=True)
class RuntimeStateSnapshot:
    values: dict
    next: tuple[str, ...]


# Shared MemorySaver so that a resume call from a different runtime instance
# (e.g. the review API endpoint) can access the original run's checkpoint.
# Single-process only; multi-process deployment needs a persistent backend
# (AsyncSqliteSaver) -- see issue 03 notes.
_SHARED_MEMORY_SAVER: Any = None


class LangGraphAgentRuntimeService(AgentRuntimeService):
    """LangGraph implementation of the Xling multi-agent workflow.

    Each conversation turn is checkpointed under thread_id = session.public_id,
    enabling crash recovery and (in issue 04) interrupt/resume for human review.
    """

    framework_name = "langgraph"

    def __init__(
        self,
        db: Session,
        settings: Settings,
        dependencies: AgentRuntimeDependencies | None = None,
    ):
        super().__init__(db, settings, dependencies)
        self._sqlite_conn = None
        self._checkpointer_ready = False
        self._checkpointer = self._make_checkpointer()
        self.graph = self._build_graph()

    async def run(self, user: UserAccount, session: ChatSession, original_input: str, model_input: str) -> AgentRunResult:
        await self._ensure_checkpointer()
        context = AgentContext(user=user, session=session, original_input=original_input, model_input=model_input)
        config = {"configurable": {"thread_id": session.public_id}}
        await self.graph.ainvoke({"context": context.to_checkpoint()}, config=config)
        await self._record_checkpoint_activity(session.public_id)
        graph_state = await self.graph.aget_state(config)
        result_context = AgentContext.from_checkpoint(
            graph_state.values.get("context", context.to_checkpoint()),
        )
        # When risk_guardian interrupts on HIGH risk, counselor has not run yet.
        # graph_state.next is non-empty (counselor pending) -> pending review.
        if graph_state.next:
            return AgentRunResult(
                intent=result_context.intent or IntentType.CHAT,
                risk_level=result_context.risk_level,
                assessment=result_context.assessment,
                retrieved_knowledge=result_context.retrieved_knowledge,
                response_messages=[],
                steps=result_context.steps,
                pending_review=True,
                trajectory_rising=result_context.trajectory_recorded,
                trajectory_trend=result_context.trajectory_trend,
                cbt_event=result_context.cbt_event,
                action_plan_event=result_context.action_plan_event,
                quick_safety_checked=result_context.quick_safety_checked,
                quick_risk_flagged=result_context.quick_risk_flagged,
            )
        return AgentRunResult(
            intent=result_context.intent or IntentType.CHAT,
            risk_level=result_context.risk_level,
            assessment=result_context.assessment,
            retrieved_knowledge=result_context.retrieved_knowledge,
            response_messages=result_context.response_messages,
            steps=result_context.steps,
            trajectory_rising=result_context.trajectory_recorded,
            trajectory_trend=result_context.trajectory_trend,
            cbt_event=result_context.cbt_event,
            action_plan_event=result_context.action_plan_event,
            quick_safety_checked=result_context.quick_safety_checked,
            quick_risk_flagged=result_context.quick_risk_flagged,
        )

    def get_state(self, thread_id: str) -> Any:
        """Return the checkpointed graph state for a thread (for debugging / resume)."""
        config = {"configurable": {"thread_id": thread_id}}
        state = self.graph.get_state(config)
        values = dict(state.values)
        if values.get("context"):
            values["context"] = AgentContext.from_checkpoint(values["context"])
        return RuntimeStateSnapshot(values=values, next=tuple(state.next))

    async def aget_state(self, thread_id: str) -> RuntimeStateSnapshot:
        await self._ensure_checkpointer()
        config = {"configurable": {"thread_id": thread_id}}
        state = await self.graph.aget_state(config)
        values = dict(state.values)
        if values.get("context"):
            values["context"] = AgentContext.from_checkpoint(values["context"])
        return RuntimeStateSnapshot(values=values, next=tuple(state.next))

    async def resume(self, thread_id: str, approved: bool) -> AgentRunResult:
        """Resume an interrupted HIGH-risk run with a counselor's decision.
        approve -> CounselorAgent generates the response; reject -> fixed fallback.
        If the checkpoint was lost (e.g. after restart, MemorySaver is in-memory),
        auto-degrade to the fallback instead of crashing (issue 08)."""
        from langgraph.types import Command

        from app.services.ai import PromptTemplates

        await self._ensure_checkpointer()
        config = {"configurable": {"thread_id": thread_id}}
        # Checkpoint lost (restart cleared MemorySaver) -> degrade to fallback
        existing_state = await self.graph.aget_state(config)
        if not existing_state.values:
            return AgentRunResult(
                intent=IntentType.CHAT,
                risk_level=RiskLevel.HIGH,
                assessment=None,
                retrieved_knowledge=[],
                response_messages=[],
                steps=[],
                pending_review=False,
                fallback_response=PromptTemplates.fallback_response(),
                degraded=True,
            )
        await self.graph.ainvoke(Command(resume={"approved": approved}), config=config)
        await self._record_checkpoint_activity(thread_id)
        graph_state = await self.graph.aget_state(config)
        result_context = AgentContext.from_checkpoint(graph_state.values["context"])
        fallback = None
        response_messages = result_context.response_messages
        if not approved:
            fallback = PromptTemplates.fallback_response()
            response_messages = []
        return AgentRunResult(
            intent=result_context.intent or IntentType.CHAT,
            risk_level=result_context.risk_level,
            assessment=result_context.assessment,
            retrieved_knowledge=result_context.retrieved_knowledge,
            response_messages=response_messages,
            steps=result_context.steps,
            pending_review=False,
            fallback_response=fallback,
        )

    def _make_checkpointer(self):
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        serde = JsonPlusSerializer()
        backend = self.settings.langgraph_checkpoint_backend.lower()
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

        global _SHARED_MEMORY_SAVER
        if _SHARED_MEMORY_SAVER is None:
            _SHARED_MEMORY_SAVER = MemorySaver(serde=serde)
        return _SHARED_MEMORY_SAVER

    async def _ensure_checkpointer(self) -> None:
        if self._checkpointer_ready:
            return
        if self._sqlite_conn is not None:
            await self._sqlite_conn
            await self._checkpointer.setup()
            await self._setup_checkpoint_retention()
        self._checkpointer_ready = True

    async def aclose(self) -> None:
        if self._sqlite_conn is not None and self._checkpointer_ready:
            await self._sqlite_conn.close()
            self._checkpointer_ready = False

    async def _setup_checkpoint_retention(self) -> None:
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
        graph.add_node("memory", self._memory_node)
        graph.add_node("quick_safety", self._quick_safety_node)
        graph.add_node("supervisor", self._supervisor_node)
        graph.add_node("knowledge", self._knowledge_node)
        graph.add_node("risk_guardian", self._risk_guardian_node)
        graph.add_node("risk_guardian_gate", self._risk_guardian_gate)
        graph.add_node("companion", self._companion_node)
        graph.add_node("counselor", self._counselor_node)
        graph.add_node("cbt", self._cbt_node)

        graph.set_entry_point("quick_safety")
        graph.add_edge("quick_safety", "memory")
        graph.add_edge("memory", "supervisor")
        graph.add_conditional_edges(
            "supervisor",
            self._route_after_supervisor,
            {"chat": "companion", "support": "risk_guardian"},
        )
        graph.add_edge("knowledge", "cbt")
        graph.add_edge("risk_guardian", "risk_guardian_gate")
        graph.add_conditional_edges(
            "risk_guardian_gate",
            self._route_after_gate,
            {"approved": "counselor", "rejected": END, "support": "knowledge"},
        )
        graph.add_conditional_edges(
            "cbt",
            self._route_after_cbt,
            {"handled": END, "skip": "counselor"},
        )
        graph.add_edge("companion", END)
        graph.add_edge("counselor", END)
        return graph.compile(checkpointer=self._checkpointer)

    async def _memory_node(self, state: GraphState) -> GraphState:
        context = self._context(state)
        await self.memory_agent(2, context)
        return {"context": context.to_checkpoint()}

    async def _quick_safety_node(self, state: GraphState) -> GraphState:
        context = self._context(state)
        await self.quick_safety_agent(1, context)
        return {"context": context.to_checkpoint()}

    async def _supervisor_node(self, state: GraphState) -> GraphState:
        context = self._context(state)
        await self.supervisor_agent(3, context)
        return {"context": context.to_checkpoint()}

    async def _knowledge_node(self, state: GraphState) -> GraphState:
        context = self._context(state)
        await self.knowledge_agent(5, context)
        return {"context": context.to_checkpoint()}

    async def _risk_guardian_node(self, state: GraphState) -> GraphState:
        context = self._context(state)
        await self.risk_guardian_agent(4, context)
        return {"context": context.to_checkpoint()}

    async def _risk_guardian_gate(self, state: GraphState) -> GraphState:
        """Pause for human review when risk is HIGH -- separate node so the
        risk_guardian assessment mutations are checkpointed before interrupt.
        On resume, interrupt() returns the counselor's decision; rejection
        sets response_planned so the conditional edge routes to END (skip counselor)."""
        context = self._context(state)
        if context.risk_level == RiskLevel.HIGH:
            from langgraph.types import interrupt

            decision = interrupt({
                "risk": "HIGH",
                "summary": context.assessment.summary if context.assessment else "",
            })
            if decision and decision.get("approved") is False:
                context.response_planned = True
        return {"context": context.to_checkpoint()}

    def _route_after_gate(self, state: GraphState) -> str:
        ctx = self._context(state)
        if ctx.response_planned:
            return "rejected"
        if ctx.risk_level == RiskLevel.HIGH:
            return "approved"
        return "support"

    def _route_after_cbt(self, state: GraphState) -> str:
        return "handled" if self._context(state).response_planned else "skip"

    async def _companion_node(self, state: GraphState) -> GraphState:
        context = self._context(state)
        await self.companion_agent(3, context)
        return {"context": context.to_checkpoint()}

    async def _cbt_node(self, state: GraphState) -> GraphState:
        context = self._context(state)
        await self.cbt_agent(5, context)
        return {"context": context.to_checkpoint()}

    async def _counselor_node(self, state: GraphState) -> GraphState:
        context = self._context(state)
        await self.counselor_agent(5, context)
        return {"context": context.to_checkpoint()}

    def _route_after_supervisor(self, state: GraphState) -> str:
        return "chat" if self._context(state).intent == IntentType.CHAT else "support"

    def _context(self, state: GraphState) -> AgentContext:
        return AgentContext.from_checkpoint(state["context"])
