"""Service for managing high-risk message review queue (issues 06-07, 11)."""
from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Callable

from sqlalchemy.orm import Session

from app.core import diagnostics
from app.core.config import Settings
from app.core.database import SessionLocal
from app.core.enums import MessageRole
from app.core.time import utc_isoformat, utc_now
from app.models.entities import ChatMessage, ChatSession, ReviewRequest, SafetyAssessmentRecord

logger = logging.getLogger(__name__)

_RISK_PRIORITY = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}

HANDOFF_REASONS = {
    "HIGH_RISK_KEYWORD", "RISK_TRAJECTORY_RISING",
    "SUSTAINED_NO_IMPROVEMENT", "USER_REQUEST", "TIMEOUT",
}

REVIEW_DECISIONS = {"approve", "reject", "refer", "monitor"}


class ReviewService:
    def __init__(self, db: Session, settings: Settings):
        """保存人工审核查询、决定和超时处理需要的数据库会话及配置。

        初始化不执行审核或恢复对话。
        """
        self.db = db
        self.settings = settings

    def list_pending(self) -> list[dict]:
        """列出待处理审核，先按风险高低，再按等待时长排序。

        排序使用风险优先级和等待秒数的负值，让默认升序实现高风险、长等待优先。
        """
        reviews = (
            self.db.query(ReviewRequest)
            .filter(ReviewRequest.status == "pending")
            .all()
        )
        items = [self._to_dict(r) for r in reviews]
        items.sort(
            # 先取风险优先级的负值，再取等待时长的负值，实现高风险和长等待优先。
            key=lambda x: (
                -_RISK_PRIORITY.get(x.get("riskLevel") or "", 0),
                -(x.get("waitSeconds") or 0),
            )
        )
        return items

    def list_all(self) -> list[dict]:
        """返回全部审核记录，待处理项优先显示。

        待处理项按风险与等待时间排序，其余按处理时间从新到旧排列。
        """
        reviews = self.db.query(ReviewRequest).all()
        items = [self._to_dict(r) for r in reviews]
        pending = [i for i in items if i.get("status") == "pending"]
        pending.sort(
            # 先取风险优先级的负值，再取等待时长的负值，实现高风险和长等待优先。
            key=lambda x: (
                -_RISK_PRIORITY.get(x.get("riskLevel") or "", 0),
                -(x.get("waitSeconds") or 0),
            )
        )
        decided = [i for i in items if i.get("status") != "pending"]
        # 按处理时间从新到旧排列已结束审核，缺少时间时使用空文本。
        decided.sort(key=lambda x: x.get("reviewedAt") or "", reverse=True)
        return pending + decided

    def escalate_timed_out(self) -> list[int]:
        """处理超过配置等待时间的审核，并保存固定安全回复。

        对候选逐条重新检查待处理状态并请求行锁；同会话已有完全相同回复时不重复写入。
        每条审核单独提交，返回成功标记为超时升级的编号列表。
        """
        from app.services.ai import PromptTemplates

        cutoff = utc_now() - timedelta(minutes=self.settings.review_timeout_minutes)
        timed_out_ids = (
            self.db.query(ReviewRequest)
            .filter(ReviewRequest.status == "pending")
            .filter(ReviewRequest.created_at < cutoff)
            .with_entities(ReviewRequest.id)
            .all()
        )
        escalated: list[int] = []
        fallback = PromptTemplates.fallback_response()
        for (review_id,) in timed_out_ids:
            review = (
                self.db.query(ReviewRequest)
                .filter(ReviewRequest.id == review_id, ReviewRequest.status == "pending")
                .with_for_update()
                .one_or_none()
            )
            # 候选列表取得后可能已被人工处理，重新检查避免覆盖新决定。
            if review is None:
                continue
            session = self.db.get(ChatSession, review.session_id)
            # 以同会话中完全相同的固定回复判断是否已经通知，避免重复超时处理写多份文字。
            already_sent = session is not None and self.db.query(ChatMessage).filter(
                ChatMessage.session_id == review.session_id,
                ChatMessage.role == MessageRole.ASSISTANT.value,
                ChatMessage.content == fallback,
            ).first() is not None
            if session is not None and not already_sent:
                self.db.add(ChatMessage(
                    user_id=session.user_id,
                    session_id=review.session_id,
                    role=MessageRole.ASSISTANT.value,
                    content=fallback,
                ))
            review.status = "escalated"
            review.reviewer_decision = "timeout"
            review.reviewer_note = "系统自动处理：人工审核等待超时"
            review.reviewed_by = "system"
            review.reviewed_at = utc_now()
            escalated.append(review.id)
            self.db.commit()
        return escalated

    async def resume_and_respond(self, review: ReviewRequest, approved: bool) -> tuple[str, bool]:
        """Restore a paused graph and observe its graph-external response and durable save."""
        from app.agents.langgraph_runtime import LangGraphAgentRuntimeService
        from app.services.ai import AiClient, PromptTemplates

        with diagnostics.execution(
            "review.request", review_id=review.id, report_id=review.report_id,
            thread_id=review.thread_id, review_action="approve" if approved else "reject",
        ):
            session = self.db.get(ChatSession, review.session_id)
            if session is None:
                raise ValueError(f"Session {review.session_id} not found for review {review.id}")
            diagnostics.bind(session_id=session.public_id)
            if review.created_at is not None:
                diagnostics.emit("review.waited", wait_ms=max(0.0, (utc_now() - review.created_at).total_seconds() * 1000))
            try:
                with diagnostics.stage("review.restore"):
                    runtime = LangGraphAgentRuntimeService(self.db, self.settings)
                    try:
                        result = await runtime.resume(review.thread_id, approved=approved)
                    finally:
                        with diagnostics.stage("runtime.close"):
                            await runtime.aclose()
            except Exception as exc:
                diagnostics.degraded("review.restore", "checkpoint_unavailable", exc)
                response_text = PromptTemplates.fallback_response()
                degraded = True
            else:
                degraded = result.degraded
                if degraded or not approved:
                    response_text = result.fallback_response or PromptTemplates.fallback_response()
                else:
                    with diagnostics.stage("review.stream"):
                        tokens = [token async for token in AiClient(self.settings).stream(result.response_messages)]
                        response_text = "".join(tokens)
            with diagnostics.stage("review.save"):
                message = ChatMessage(
                    user_id=session.user_id, session_id=session.id,
                    role=MessageRole.ASSISTANT.value, content=response_text,
                )
                self.db.add(message)
                self.db.flush()
                message_id = message.id
                self.db.commit()
                diagnostics.bind(assistant_message_id=message_id)
                diagnostics.emit("review.persisted", assistant_message_id=message_id)
            return response_text, degraded

    def mark_escalated(self, review_id: int) -> ReviewRequest:
        """将仍待处理的审核标记为已安全升级并保存处理时间。

        不存在或已处理的记录由统一检查抛错；不在此发送回复。
        """
        review = self._get_pending(review_id)
        review.status = "escalated"
        review.reviewed_at = utc_now()
        self.db.commit()
        return review


    def create_with_context(
        self,
        session_id: int,
        report_id: int,
        thread_id: str,
        risk_summary: str = "",
        handoff_reason: str = "HIGH_RISK_KEYWORD",
        desensitized_summary: str = "",
    ) -> ReviewRequest:
        """创建包含触发原因和脱敏摘要的待处理审核记录。

        先验证原因，再写入会话、评估记录和执行编号，确认保存后返回刷新对象。
        """
        if handoff_reason not in HANDOFF_REASONS:
            raise ValueError(f"Invalid handoff reason: {handoff_reason}")
        review = ReviewRequest(
            session_id=session_id,
            report_id=report_id,
            thread_id=thread_id,
            risk_summary=risk_summary,
            handoff_reason=handoff_reason,
            desensitized_summary=desensitized_summary,
            status="pending",
        )
        self.db.add(review)
        self.db.commit()
        self.db.refresh(review)
        return review

    def mark_decision(
        self,
        review_id: int,
        decision: str,
        note: str = "",
        reviewed_by: str = "",
        referral_target: str | None = None,
        next_step: str | None = None,
        follow_up_owner: str | None = None,
        follow_up_at: datetime | None = None,
    ) -> ReviewRequest:
        """验证并保存审核决定、处理人员及后续行动详情。

        转介必须填写去向和下一步，持续关注必须填写负责人和时间；仅可修改待处理记录。
        """
        if decision not in REVIEW_DECISIONS:
            raise ValueError(f"Invalid decision: {decision}. Valid: {REVIEW_DECISIONS}")
        referral_target = referral_target.strip() if referral_target else None
        next_step = next_step.strip() if next_step else None
        follow_up_owner = follow_up_owner.strip() if follow_up_owner else None
        # 转介必须有实际去向与下一步，持续关注则必须有人负责且有约定时间。
        if decision == "refer" and (not referral_target or not next_step):
            raise ValueError("referral_target and next_step are required for refer")
        if decision == "monitor" and (not follow_up_owner or follow_up_at is None):
            raise ValueError("follow_up_owner and follow_up_at are required for monitor")
        review = self._get_pending(review_id)
        review.reviewer_decision = decision
        review.reviewer_note = note
        review.reviewed_by = reviewed_by
        review.referral_target = referral_target
        review.next_step = next_step
        review.follow_up_owner = follow_up_owner
        review.follow_up_at = (
            follow_up_at.astimezone(UTC).replace(tzinfo=None)
            if follow_up_at is not None and follow_up_at.tzinfo is not None
            else follow_up_at
        )
        review.reviewed_at = utc_now()
        # 将审核决定映射成对外展示和后续查询使用的处理状态。
        status_map = {"approve": "approved", "reject": "rejected", "refer": "referred", "monitor": "monitoring"}
        review.status = status_map[decision]
        self.db.commit()
        return review

    def get_review(self, review_id: int) -> ReviewRequest | None:
        """按内部编号读取审核记录，缺失时返回 None。

        本服务方法不负责接口权限验证，也不改变审核状态。
        """
        return self.db.get(ReviewRequest, review_id)

    @staticmethod
    def student_message(review: ReviewRequest) -> str:
        """为转介或持续关注决定生成用户可见的后续行动说明。

        其他决定返回空字符串；只构造文字，不写入数据库或发送外部消息。
        """
        if review.reviewer_decision == "refer":
            return (
                f"人工审核建议你联系{review.referral_target}。"
                f"下一步：{review.next_step}。如你现在处于紧急危险中，请立即联系身边可信任的人或当地紧急服务。"
            )
        if review.reviewer_decision == "monitor":
            when = review.follow_up_at.strftime("%Y-%m-%d %H:%M UTC") if review.follow_up_at else "后续约定时间"
            return f"人工审核记录了持续关注建议。负责人：{review.follow_up_owner}；建议跟进时间：{when}。如情况变化，请主动联系身边可信任的人或当地支持服务。"
        return ""

    def persist_student_message(self, review: ReviewRequest) -> str:
        """将审核决定对应的后续行动说明保存成会话消息。

        没有说明或原会话已不存在时返回空字符串；成功提交后返回保存的文字。
        """
        message = self.student_message(review)
        if not message:
            return ""
        session = self.db.get(ChatSession, review.session_id)
        if session is None:
            return ""
        self.db.add(ChatMessage(
            user_id=session.user_id,
            session_id=session.id,
            role=MessageRole.ASSISTANT.value,
            content=message,
        ))
        self.db.commit()
        return message

    def _get_pending(self, review_id: int) -> ReviewRequest:
        """读取审核记录并要求它仍处于待处理状态。

        不存在或已经处理时抛出 ValueError，防止把普通更新误用到已结束记录。
        """
        review = self.db.get(ReviewRequest, review_id)
        if review is None:
            raise ValueError(f"Review {review_id} not found")
        if review.status != "pending":
            raise ValueError(f"Review {review_id} is not pending (status={review.status})")
        return review

    def _to_dict(self, review: ReviewRequest) -> dict:
        """为审核列表组织评估信息、处理详情和最近十条会话消息。

        最近消息先倒序截取再恢复时间顺序；等待时长按当前时间减创建时间计算。
        当前返回中包含原消息与最近上下文，不能将整个返回值描述为已全面脱敏。
        """
        report = self.db.get(SafetyAssessmentRecord, review.report_id)
        messages = (
            self.db.query(ChatMessage)
            .filter(ChatMessage.session_id == review.session_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(10)
            .all()
        )
        messages.reverse()
        now = utc_now()
        return {
            "reviewId": review.id,
            "threadId": review.thread_id,
            "reportId": review.report_id,
            "sessionId": review.session_id,
            "riskSummary": review.risk_summary,
            "handoffReason": review.handoff_reason,
            "desensitizedSummary": review.desensitized_summary,
            "reviewerDecision": review.reviewer_decision,
            "reviewerNote": review.reviewer_note,
            "reviewedBy": review.reviewed_by,
            "referralTarget": review.referral_target,
            "nextStep": review.next_step,
            "followUpOwner": review.follow_up_owner,
            "followUpAt": utc_isoformat(review.follow_up_at) if review.follow_up_at else None,
            "status": review.status,
            "reviewedAt": utc_isoformat(review.reviewed_at) if review.reviewed_at else None,
            "riskLevel": report.risk_level if report else None,
            "emotion": report.emotion if report else None,
            "emotionScore": report.emotion_score if report else None,
            "studentMessage": report.content if report else None,
            "recentContext": [{"role": m.role, "content": m.content} for m in messages],
            "createdAt": utc_isoformat(review.created_at) if review.created_at else None,
            "waitSeconds": (now - review.created_at).total_seconds() if review.created_at else None,
        }


class ReviewTimeoutWorker:
    """独立检查人工审核等待超时，不依赖后台页面被打开。"""

    def __init__(self, settings: Settings, session_factory: Callable[[], Session] = SessionLocal):
        """准备独立审核超时循环及数据库会话创建函数。

        可替换 session_factory 进行隔离测试，初始化不启动线程。
        """
        self.settings = settings
        self.session_factory = session_factory
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        """在尚未启动时清除停止标志并创建后台线程。

        已有线程引用时跳过，避免重复启动同一工作器。
        """
        if self.thread is not None:
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="xling-review-timeouts", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        """通知超时循环退出并最多等待五秒。

        清除线程引用不代表阻塞中的数据库操作被强制终止。
        """
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None

    def run_once(self) -> list[int]:
        """用独立数据库会话执行一轮超时审核处理。

        返回升级编号列表，并在成功或异常后关闭会话。
        """
        db = self.session_factory()
        try:
            return ReviewService(db, self.settings).escalate_timed_out()
        finally:
            db.close()

    def _loop(self) -> None:
        """按至少一秒的间隔重复检查审核超时。

        单轮失败记录异常后等待下一轮重试，停止事件可提前结束等待。
        """
        interval = max(1.0, self.settings.review_timeout_poll_interval_seconds)
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception as exc:
                diagnostics.degraded("review.timeout", "timeout_worker_failed", exc)
            self.stop_event.wait(interval)


_timeout_worker: ReviewTimeoutWorker | None = None


def get_review_timeout_worker(settings: Settings) -> ReviewTimeoutWorker:
    """取得当前进程共享的审核超时工作器。

    首次调用创建实例，后续返回同一实例，因此不会自动应用新的配置对象。
    """
    global _timeout_worker
    if _timeout_worker is None:
        _timeout_worker = ReviewTimeoutWorker(settings)
    return _timeout_worker
