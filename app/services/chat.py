from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.agents.factory import create_agent_runtime
from app.agents.runtime import ActionPlanEvent, CbtEvent
from app.core.config import Settings
from app.core.enums import MessageRole
from app.models.entities import ChatMessage, ChatSession, PsychologicalReport, UserAccount
from app.schemas.dtos import AiMessage, ChatRequest, ChatStreamEvent
from app.services.ai import AiClient, PromptTemplates
from app.services.memory import RedisShortTermMemoryStore
from app.services.mcp_client import McpToolError, XlingMcpToolClient
from app.services.privacy import PrivacySanitizer
from app.services.review import ReviewService
from app.services.tool_queue import ToolQueueService


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


class ChatService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.privacy = PrivacySanitizer()
        self.memory = RedisShortTermMemoryStore(settings)
        self.ai = AiClient(settings)

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
            if prepared.report_id is not None and self.settings.tool_queue_enabled:
                ToolQueueService(self.db, self.settings).enqueue_report(prepared.report_id, prepared.risk_level)
            yield sse(
                "pending_review",
                ChatStreamEvent(type="pending_review", sessionId=session_id, content=ack).model_dump(),
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
            if self.settings.tool_queue_enabled:
                ToolQueueService(self.db, self.settings).enqueue_report(prepared.report_id, prepared.risk_level)
            else:
                try:
                    await XlingMcpToolClient(self.settings).handle_report(prepared.report_id, prepared.risk_level)
                except McpToolError as exc:
                    yield sse(
                        "error",
                        ChatStreamEvent(
                            type="error",
                            sessionId=session_id,
                            message=f"MCP 工具调用失败：{exc}",
                        ).model_dump(),
                    )
                    return
        yield sse("done", ChatStreamEvent(type="done", sessionId=session_id).model_dump())

    async def prepare(self, user: UserAccount, request: ChatRequest) -> PreparedChat:
        text = request.message.strip()
        model_input = self.privacy.sanitize(text)
        session = self.resolve_session(user, request.sessionId, text, request.noMemory)
        agent_run = await create_agent_runtime(self.db, self.settings).run(user, session, text, model_input)
        self.save_message(user, session, MessageRole.USER, text)
        report_id = None
        if agent_run.requires_report and agent_run.assessment is not None:
            report = PsychologicalReport(
                user_id=user.id,
                session_id=session.id,
                content=text,
                intent=agent_run.intent.value,
                emotion=agent_run.assessment.emotion.value,
                emotion_score=agent_run.assessment.emotion_score,
                risk_level=agent_run.risk_level.value,
                confidence=agent_run.assessment.confidence,
                summary=agent_run.assessment.summary,
            )
            self.db.add(report)
            self.db.commit()
            report_id = report.id
            risk_level = report.risk_level
            if agent_run.pending_review:
                if agent_run.trajectory_rising:
                    handoff_reason = "RISK_TRAJECTORY_RISING"
                    risk_trend = agent_run.trajectory_trend or "风险轨迹连续上升"
                else:
                    handoff_reason = "HIGH_RISK_KEYWORD"
                    risk_trend = "单条消息达到高风险"
                ReviewService(self.db, self.settings).create_with_context(
                    session_id=session.id,
                    report_id=report_id,
                    thread_id=session.public_id,
                    risk_summary=agent_run.assessment.summary,
                    handoff_reason=handoff_reason,
                    desensitized_summary=self.privacy.build_review_summary(
                        current_difficulty=text,
                        risk_trend=risk_trend,
                    ),
                )
        else:
            risk_level = None
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
        self.db.commit()
        self.db.refresh(session)
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
