from __future__ import annotations

import json
import uuid
from contextlib import aclosing
from dataclasses import dataclass
from typing import Callable

from sqlalchemy.orm import Session

from app.agents.factory import create_agent_runtime
from app.agents.runtime import ActionPlanEvent, AgentRunResult, AgentRuntime, CbtEvent
from app.core import diagnostics
from app.core.config import Settings
from app.core.enums import MessageRole
from app.models.entities import ChatMessage, ChatSession, ReviewRequest, UserAccount
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


RuntimeFactory = Callable[[Session, Settings], AgentRuntime]


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
        """创建聊天服务的默认模型、记忆、脱敏器和报告调度依赖。

        runtime_factory 保存执行器创建函数；observe_run 的匿名函数默认不做处理，供测试或观察器替换。
        """
        return cls(
            ai=AiClient(settings),
            memory=RedisShortTermMemoryStore(settings),
            privacy=PrivacySanitizer(),
            runtime_factory=create_agent_runtime,
            report_dispatcher=create_report_dispatcher(db, settings),
            # 默认运行观察器不执行操作；测试可替换它以记录运行结果。
            observe_run=lambda run: None,
        )


class PendingReviewError(ValueError):
    """The conversation is paused until its review has completed."""


class ChatService:
    def __init__(
        self,
        db: Session,
        settings: Settings,
        dependencies: ChatDependencies | None = None,
    ):
        """装配聊天入口依赖，并创建统一保存本轮数据的服务。

        db、settings 由请求提供；传入 dependencies 可替换外部服务，初始化不直接开始对话。
        """
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
        """Stream business events while observing the entire request, including graph-external work."""
        with diagnostics.execution("chat.request", session_id=request.sessionId):
            with diagnostics.stage("chat.prepare"):
                prepared = await self.prepare(user, request)
            session_id = prepared.session.public_id
            diagnostics.bind(session_id=session_id, thread_id=session_id)
            yield sse(
                "meta",
                ChatStreamEvent(type="meta", sessionId=session_id, noMemory=prepared.session.no_memory).model_dump(),
            )
            if prepared.pending_review:
                ack = PromptTemplates.crisis_acknowledgment()
                self.save_message(user, prepared.session, MessageRole.ASSISTANT, ack)
                diagnostics.emit("chat.waiting_review", requires_review=True)
                yield sse("pending_review", ChatStreamEvent(
                    type="pending_review", sessionId=session_id, content=ack,
                ).model_dump())
                if prepared.report_id is not None:
                    error = await self._dispatch_report(prepared.report_id, prepared.risk_level)
                    if error:
                        yield sse("error", ChatStreamEvent(type="error", sessionId=session_id, message=error).model_dump())
                yield sse("done", ChatStreamEvent(type="done", sessionId=session_id).model_dump())
                return
            if prepared.cbt_event is not None:
                yield sse("cbt", {"type": "cbt", "sessionId": session_id, **prepared.cbt_event})
            if prepared.action_plan_event is not None:
                yield sse("action_plan", {"type": "action_plan", "sessionId": session_id, **prepared.action_plan_event})
            assistant = []
            with diagnostics.stage("chat.stream"):
                async with aclosing(self.ai.stream(prepared.messages)) as stream:
                    async for token in stream:
                        assistant.append(token)
                        yield sse("token", ChatStreamEvent(type="token", sessionId=session_id, content=token).model_dump())
            if assistant:
                self.save_message(user, prepared.session, MessageRole.ASSISTANT, "".join(assistant))
            if prepared.report_id is not None:
                error = await self._dispatch_report(prepared.report_id, prepared.risk_level)
                if error:
                    yield sse("error", ChatStreamEvent(type="error", sessionId=session_id, message=error).model_dump())
                    return
            yield sse("done", ChatStreamEvent(type="done", sessionId=session_id).model_dump())

    async def prepare(self, user: UserAccount, request: ChatRequest) -> PreparedChat:
        """完成本轮脱敏、会话定位、对话执行和统一数据保存。

        原文用于业务记录，脱敏文本供模型使用；执行器无论成功失败都尝试关闭异步资源。
        数据库保存成功后才追加短期记忆，返回后续推送需要的明确字段。
        """
        self.ensure_chat_available(user, request.sessionId)
        text = request.message.strip()
        model_input = self.privacy.sanitize(text)
        with diagnostics.stage("chat.session"):
            session = self.resolve_session(user, request.sessionId, text, request.noMemory)
        diagnostics.bind(session_id=session.public_id, thread_id=session.public_id)
        with diagnostics.stage("runtime.create"):
            runtime = self.runtime_factory(self.db, self.settings)
        try:
            agent_run = await runtime.run(user, session, text, model_input)
        finally:
            close = getattr(runtime, "aclose", None)
            if close is not None:
                with diagnostics.stage("runtime.close"):
                    await close()
        self.observe_run(agent_run)
        if agent_run.trajectory_rising:
            handoff_reason = "RISK_TRAJECTORY_RISING"
            risk_trend = agent_run.trajectory_trend or "风险轨迹连续上升"
        else:
            handoff_reason = "HIGH_RISK_KEYWORD"
            risk_trend = "单条消息达到高风险"
        with diagnostics.stage("support_turn.save"):
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
        # 先确认数据库保存成功，再更新短期缓存，以数据库记录作为后续恢复来源。
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
        """触发安全评估记录的后续工具处理。

        report_id 指定记录；约定的调度异常转成可发送的错误文字，成功返回 None，其他异常不在此捕获。
        """
        try:
            with diagnostics.stage("chat.dispatch", report_id=report_id):
                await self.report_dispatcher.dispatch(report_id, risk_level)
        except ReportDispatchError as exc:
            diagnostics.degraded("chat.dispatch", "report_dispatch_failed", exc, report_id=report_id)
            return f"报告后处理失败：{exc}"
        return None

    def resolve_session(
        self, user: UserAccount, public_id: str | None, text: str, no_memory: bool | None = None
    ) -> ChatSession:
        """校验并复用当前用户的已有会话，或准备一条新会话记录。

        已有会话保留创建时的无记忆设置；新会话使用首条消息前 36 个字符作为标题。
        新建时只 flush 取得编号，不提前确认保存，便于随后与本轮消息一起提交。
        """
        if public_id:
            session = self.db.query(ChatSession).filter(ChatSession.public_id == public_id, ChatSession.user_id == user.id).first()
            if session is None:
                raise ValueError("Session not found")
            # 已有会话保持创建时的无记忆设置，响应事件会把实际保存值告知网页。
            return session
        session = ChatSession(
            public_id=uuid.uuid4().hex, user_id=user.id, title=text[:36], no_memory=bool(no_memory)
        )
        self.db.add(session)
        self.db.flush()
        return session

    def ensure_chat_available(self, user: UserAccount, public_id: str | None) -> None:
        if public_id and self.db.query(ReviewRequest.id).join(
            ChatSession, ReviewRequest.session_id == ChatSession.id,
        ).filter(
            ChatSession.public_id == public_id,
            ChatSession.user_id == user.id,
            ReviewRequest.status == "pending",
        ).first():
            raise PendingReviewError("当前会话正在等待审核，请等待审核完成或创建新会话。")

    def save_message(self, user: UserAccount, session: ChatSession, role: MessageRole, content: str) -> None:
        """保存一条消息并更新会话时间，再同步到短期缓存。

        role 指定用户或助手角色；数据库确认保存后才追加缓存，两种存储不构成同一事务。
        """
        with diagnostics.stage("chat.save"):
            message = ChatMessage(user_id=user.id, session_id=session.id, role=role.value, content=content)
            self.db.add(message)
            session.touch()
            self.db.add(session)
            self.db.flush()
            message_id = message.id
            self.db.commit()
            diagnostics.bind(assistant_message_id=message_id)
            diagnostics.emit("chat.persisted", assistant_message_id=message_id)
            self.memory.append(session.public_id, role.value, content)


def _cbt_payload(event: CbtEvent) -> dict:
    """将四维追问事件转换成网页约定的字段名。

    直接读取事件对象，不从人类阅读的日志文字推断进度。
    """
    return {
        "active": event.active,
        "completedCount": event.completed_count,
        "nextDimension": event.next_dimension,
        "complete": event.complete,
    }


def _action_plan_payload(event: ActionPlanEvent) -> dict:
    """把新生成的行动计划事件转换成网页数据。

    包含计划编号、反馈时间及有序条目；此事件表示新计划可提交反馈。
    """
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
    """把事件名称和字典内容编码成服务端持续推送的文本格式。

    SSE（服务器向网页连续发送事件的格式）使用空行分隔事件；中文直接保留，特殊对象通过 str 转成文本。
    """
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
