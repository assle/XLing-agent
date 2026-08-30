from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Callable

from sqlalchemy.orm import Session

from app.agents.factory import create_agent_runtime
from app.agents.runtime import ActionPlanEvent, AgentRunResult, AgentRuntimeService, CbtEvent
from app.core.config import Settings
from app.core.enums import MessageRole
from app.models.entities import ChatMessage, ChatSession, UserAccount
from app.schemas.dtos import AiMessage, ChatRequest, ChatStreamEvent
from app.services.ai import AiClient, PromptTemplates
from app.services.memory import RedisShortTermMemoryStore
from app.services.privacy import PrivacySanitizer
from app.services.report_dispatch import (
    ReportDispatcher,
    ReportDispatchError,
    create_report_dispatcher,
)
from app.services.support_turn import SupportTurnTransaction


@dataclass
class PreparedChat:
    """Everything stream_chat needs for one turn, as typed fields."""
    session: ChatSession
    messages: list[AiMessage]
    report_id: int | None
    risk_level: str | None
    pending_review: bool
    cbt_event: dict | None
    action_plan_event: dict | None


RuntimeFactory = Callable[[Session, Settings], AgentRuntimeService]


@dataclass(frozen=True)
class ChatDependencies:
    """Internal collaborators behind the chat interface."""

    ai: AiClient
    memory: RedisShortTermMemoryStore
    privacy: PrivacySanitizer
    runtime_factory: RuntimeFactory
    report_dispatcher: ReportDispatcher
    observe_run: Callable[[AgentRunResult], None]

    @classmethod
    def create(cls, db: Session, settings: Settings) -> ChatDependencies:
        return cls(
            ai=AiClient(settings),
            memory=RedisShortTermMemoryStore(settings),
            privacy=PrivacySanitizer(),
            runtime_factory=create_agent_runtime,
            report_dispatcher=create_report_dispatcher(db, settings),
            observe_run=lambda run: None,
        )


class ChatService:
    def __init__(
        self,
        db: Session,
        settings: Settings,
        dependencies: ChatDependencies | None = None,
    ):
        self.db = db
        self.settings = settings
        dependencies = dependencies or ChatDependencies.create(db, settings)
        self.privacy = dependencies.privacy
        self.memory = dependencies.memory
        self.ai = dependencies.ai
        self.runtime_factory = dependencies.runtime_factory
        self.report_dispatcher = dependencies.report_dispatcher
        self.observe_run = dependencies.observe_run
        self.turns = SupportTurnTransaction(db, settings)

    async def stream_chat(self, user: UserAccount, request: ChatRequest):
        prepared = await self.prepare(user, request)
        session_id = prepared.session.public_id
        yield sse(
            "meta",
            ChatStreamEvent(
                type="meta", sessionId=session_id, noMemory=prepared.session.no_memory
            ).model_dump(by_alias=True),
        )
        if prepared.pending_review:
            ack = PromptTemplates.crisis_acknowledgment()
            self.save_message(user, prepared.session, MessageRole.ASSISTANT, ack)
            yield sse(
                "pending_review",
                ChatStreamEvent(type="pending_review", sessionId=session_id, content=ack).model_dump(),
            )
            if prepared.report_id is not None:
                error = await self._dispatch_report(prepared.report_id, prepared.risk_level)
                if error:
                    yield sse(
                        "error",
                        ChatStreamEvent(type="error", sessionId=session_id, message=error).model_dump(),
                    )
            yield sse("done", ChatStreamEvent(type="done", sessionId=session_id).model_dump())
            return
        if prepared.cbt_event is not None:
            yield sse("cbt", {"type": "cbt", "sessionId": session_id, **prepared.cbt_event})
        if prepared.action_plan_event is not None:
            yield sse("action_plan", {"type": "action_plan", "sessionId": session_id, **prepared.action_plan_event})
        assistant = []
        async for token in self.ai.stream(prepared.messages):
            assistant.append(token)
            yield sse("token", ChatStreamEvent(type="token", sessionId=session_id, content=token).model_dump())
        if assistant:
            self.save_message(user, prepared.session, MessageRole.ASSISTANT, "".join(assistant))
        if prepared.report_id is not None:
            error = await self._dispatch_report(prepared.report_id, prepared.risk_level)
            if error:
                yield sse(
                    "error",
                    ChatStreamEvent(type="error", sessionId=session_id, message=error).model_dump(),
                )
                return
        yield sse("done", ChatStreamEvent(type="done", sessionId=session_id).model_dump())

    async def prepare(self, user: UserAccount, request: ChatRequest) -> PreparedChat:
        text = request.message.strip()
        model_input = self.privacy.sanitize(text)
        session = self.resolve_session(user, request.sessionId, text, request.noMemory)
        runtime = self.runtime_factory(self.db, self.settings)
        try:
            agent_run = await runtime.run(user, session, text, model_input)
        finally:
            close = getattr(runtime, "aclose", None)
            if close is not None:
                await close()
        self.observe_run(agent_run)
        if agent_run.trajectory_rising:
            handoff_reason = "RISK_TRAJECTORY_RISING"
            risk_trend = agent_run.trajectory_trend or "风险轨迹连续上升"
        else:
            handoff_reason = "HIGH_RISK_KEYWORD"
            risk_trend = "单条消息达到高风险"
        persisted = self.turns.save_support_turn(
            user=user,
            session=session,
            content=text,
            run=agent_run,
            handoff_reason=handoff_reason,
            desensitized_summary=self.privacy.build_review_summary(
                current_difficulty=text,
                risk_trend=risk_trend,
            ),
        )
        self.memory.append(session.public_id, MessageRole.USER.value, text)
        report_id = persisted.report.id if persisted.report else None
        risk_level = persisted.report.risk_level if persisted.report else None
        return PreparedChat(
            session=session,
            messages=agent_run.response_messages,
            report_id=report_id,
            risk_level=risk_level,
            pending_review=agent_run.pending_review,
            cbt_event=_cbt_payload(agent_run.cbt_event) if agent_run.cbt_event else None,
            action_plan_event=(
                _action_plan_payload(agent_run.action_plan_event) if agent_run.action_plan_event else None
            ),
        )

    async def _dispatch_report(self, report_id: int, risk_level: str | None) -> str | None:
        try:
            await self.report_dispatcher.dispatch(report_id, risk_level)
        except ReportDispatchError as exc:
            return f"报告后处理失败：{exc}"
        return None

    def resolve_session(
        self, user: UserAccount, public_id: str | None, text: str, no_memory: bool | None = None
    ) -> ChatSession:
        if public_id:
            session = self.db.query(ChatSession).filter(ChatSession.public_id == public_id, ChatSession.user_id == user.id).first()
            if session is None:
                raise ValueError("Session not found")
            # Session privacy mode is immutable after creation. The response
            # metadata reports the persisted mode back to the client.
            return session
        session = ChatSession(
            public_id=uuid.uuid4().hex, user_id=user.id, title=text[:36], no_memory=bool(no_memory)
        )
        self.db.add(session)
        self.db.flush()
        return session

    def save_message(self, user: UserAccount, session: ChatSession, role: MessageRole, content: str) -> None:
        self.db.add(ChatMessage(user_id=user.id, session_id=session.id, role=role.value, content=content))
        session.touch()
        self.db.add(session)
        self.db.commit()
        self.memory.append(session.public_id, role.value, content)

def _cbt_payload(event: CbtEvent) -> dict:
    """Wire format (camelCase) for the SSE `cbt` event."""
    return {
        "active": event.active,
        "completedCount": event.completed_count,
        "nextDimension": event.next_dimension,
        "complete": event.complete,
    }


def _action_plan_payload(event: ActionPlanEvent) -> dict:
    """Wire format (camelCase) for the SSE `action_plan` event."""
    return {
        "planId": event.plan_id,
        "feedbackDueAt": event.feedback_due_at,
        "feedbackAvailable": True,
        "items": [
            {"id": item.id, "content": item.content, "order": item.order, "completed": item.completed}
            for item in event.items
        ],
    }


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
