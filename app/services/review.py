"""Service for managing high-risk message review queue (issues 06-07, 11)."""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable

from sqlalchemy.orm import Session

from app.core import diagnostics
from app.core.config import Settings
from app.core.database import SessionLocal
from app.core.enums import MessageRole, ToolJobKind, ToolJobStatus
from app.core.time import utc_isoformat, utc_now
from app.models.entities import (
    ChatMessage,
    ChatSession,
    OffsiteContactRecord,
    ReviewRequest,
    SafetyAssessmentRecord,
    SupportAuditEvent,
    ToolJob,
)

logger = logging.getLogger(__name__)

_RISK_PRIORITY = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}

HANDOFF_REASONS = {
    "HIGH_RISK_KEYWORD", "RISK_TRAJECTORY_RISING",
    "SUSTAINED_NO_IMPROVEMENT", "USER_REQUEST", "TIMEOUT",
}

REVIEW_DECISIONS = {"refer", "monitor", "complete"}


class ReviewConflictError(ValueError):
    """人工支持记录已被其他操作推进，当前请求不能覆盖它。"""


@dataclass(frozen=True)
class ReviewOutcome:
    """一次人工处置的完整输入，集中负责规范化和必填信息校验。"""

    decision: str
    note: str = ""
    referral_target: str | None = None
    next_step: str | None = None
    follow_up_owner: str | None = None
    follow_up_at: datetime | None = None
    completion_basis: str | None = None

    def __post_init__(self) -> None:
        decision = self.decision.strip().lower()
        if decision not in REVIEW_DECISIONS:
            raise ValueError(f"Invalid decision: {decision}. Valid: {REVIEW_DECISIONS}")
        object.__setattr__(self, "decision", decision)
        object.__setattr__(self, "note", self.note.strip())
        for field_name in ("referral_target", "next_step", "follow_up_owner", "completion_basis"):
            value = getattr(self, field_name)
            object.__setattr__(self, field_name, value.strip() if value else None)
        if self.follow_up_at is not None and self.follow_up_at.tzinfo is not None:
            object.__setattr__(
                self, "follow_up_at", self.follow_up_at.astimezone(UTC).replace(tzinfo=None),
            )
        if decision == "refer" and (not self.referral_target or not self.next_step or not self.follow_up_owner):
            raise ValueError("referral_target, next_step and follow_up_owner are required for refer")
        if decision == "monitor" and (not self.follow_up_owner or self.follow_up_at is None):
            raise ValueError("follow_up_owner and follow_up_at are required for monitor")
        if decision == "complete" and (not self.note or not self.completion_basis):
            raise ValueError("note and completion_basis are required for complete")

    @property
    def status(self) -> str:
        return {"refer": "referred", "monitor": "monitoring", "complete": "completed"}[self.decision]


class ReviewService:
    def __init__(
        self, db: Session, settings: Settings, *, fault_injector: Callable[[str], None] | None = None,
    ):
        """保存人工支持依赖；故障入口仅供事务测试在提交前注入异常。"""
        self.db = db
        self.settings = settings
        self.fault_injector = fault_injector or (lambda stage: None)

    def claim(self, review_id: int, claimed_by: str) -> ReviewRequest:
        """用一次条件更新原子接手待处理需求，并在同一事务写入审计。"""
        now = utc_now()
        try:
            claimed = (
                self.db.query(ReviewRequest)
                .filter(ReviewRequest.id == review_id, ReviewRequest.status == "pending")
                .update(
                    {
                        ReviewRequest.status: "in_progress",
                        ReviewRequest.claimed_by: claimed_by,
                        ReviewRequest.claimed_at: now,
                    },
                    synchronize_session=False,
                )
            )
            if claimed != 1:
                self.db.rollback()
                if self.db.get(ReviewRequest, review_id) is None:
                    raise ValueError(f"Review {review_id} not found")
                raise ReviewConflictError(f"Review {review_id} is not pending")
            self.db.add(SupportAuditEvent(
                review_id=review_id, event_type="claimed", actor=claimed_by, payload_json="{}",
            ))
            self.db.commit()
            review = self.db.get(ReviewRequest, review_id)
            self.db.refresh(review)
            return review
        except Exception:
            self.db.rollback()
            raise

    def reply(
        self, review_id: int, content: str, replied_by: str, client_request_id: str | None = None,
    ) -> ChatMessage:
        """由当前负责人发送站内人工回复；相同客户端请求不会重复保存。"""
        content = content.strip()
        if not content:
            raise ValueError("content is required")
        try:
            review = self._get_in_progress(review_id, replied_by)
            if client_request_id:
                duplicate = self.db.query(ChatMessage).filter(
                    ChatMessage.review_id == review.id,
                    ChatMessage.role == MessageRole.SUPPORT.value,
                    ChatMessage.client_request_id == client_request_id,
                ).first()
                if duplicate is not None:
                    self.db.rollback()
                    return duplicate
            return self._append_support_message_and_audit(
                review=review, actor=replied_by, event_type="human_reply", content=content,
                client_request_id=client_request_id, fault_stage="human_reply",
            )
        except Exception:
            self.db.rollback()
            raise

    def record_offsite_contact(
        self,
        review_id: int,
        *,
        contact_method: str,
        contacted_at: datetime,
        outcome: str,
        next_step: str,
        recorded_by: str,
        client_request_id: str | None = None,
    ) -> OffsiteContactRecord:
        """保存站外联系记录及用户可见下一步，不记录通话正文。"""
        contact_method = contact_method.strip()
        outcome = outcome.strip()
        next_step = next_step.strip()
        if not contact_method or not outcome or not next_step:
            raise ValueError("contact_method, outcome and next_step are required")
        if contacted_at.tzinfo is not None:
            contacted_at = contacted_at.astimezone(UTC).replace(tzinfo=None)
        try:
            review = self._get_in_progress(review_id, recorded_by)
            if client_request_id:
                duplicate = self.db.query(OffsiteContactRecord).filter(
                    OffsiteContactRecord.review_id == review.id,
                    OffsiteContactRecord.client_request_id == client_request_id,
                ).first()
                if duplicate is not None:
                    self.db.rollback()
                    return duplicate
            contact = OffsiteContactRecord(
                review_id=review.id, contact_method=contact_method, contacted_at=contacted_at,
                outcome=outcome, next_step=next_step, recorded_by=recorded_by,
                client_request_id=client_request_id,
            )
            self.db.add(contact)
            self.db.flush()
            user_message = (
                f"支持人员新增了站外联系记录。方式：{contact_method}；"
                f"时间：{utc_isoformat(contacted_at)}；结果：{outcome}；下一步：{next_step}。"
            )
            self._append_support_message_and_audit(
                review=review, actor=recorded_by, event_type="offsite_contact", content=user_message,
                client_request_id=client_request_id, audit_payload={"contactId": contact.id},
                fault_stage="offsite_contact",
            )
            self.db.refresh(contact)
            return contact
        except Exception:
            self.db.rollback()
            raise

    def list_active(self) -> list[dict]:
        """列出等待接手和处理中记录，先按风险高低，再按等待时长排序。"""
        reviews = (
            self.db.query(ReviewRequest)
            .filter(ReviewRequest.status.in_(["pending", "in_progress"]))
            .all()
        )
        items = [self._to_dict(r) for r in reviews]
        items.sort(
            key=lambda x: (
                -_RISK_PRIORITY.get(x.get("riskLevel") or "", 0),
                -(x.get("waitSeconds") or 0),
            )
        )
        return items

    def list_all(self) -> list[dict]:
        """返回全部记录，活跃人工支持优先，终结记录按处理时间排列。"""
        reviews = self.db.query(ReviewRequest).all()
        items = [self._to_dict(r) for r in reviews]
        active = [i for i in items if i.get("status") in {"pending", "in_progress"}]
        active.sort(
            key=lambda x: (
                -_RISK_PRIORITY.get(x.get("riskLevel") or "", 0),
                -(x.get("waitSeconds") or 0),
            )
        )
        terminal = [i for i in items if i.get("status") not in {"pending", "in_progress"}]
        terminal.sort(key=lambda x: x.get("reviewedAt") or "", reverse=True)
        return active + terminal

    def escalate_timed_out(self) -> list[int]:
        """超时只登记升级与通知任务，需求和负责人保持原状态。"""
        cutoff = utc_now() - timedelta(minutes=self.settings.review_timeout_minutes)
        timed_out_ids = (
            self.db.query(ReviewRequest)
            .filter(ReviewRequest.status.in_(["pending", "in_progress"]))
            .filter(ReviewRequest.created_at < cutoff)
            .filter(ReviewRequest.last_escalated_at.is_(None))
            .with_entities(ReviewRequest.id)
            .all()
        )
        escalated: list[int] = []
        for (review_id,) in timed_out_ids:
            try:
                review = (
                    self.db.query(ReviewRequest)
                    .filter(
                        ReviewRequest.id == review_id,
                        ReviewRequest.status.in_(["pending", "in_progress"]),
                        ReviewRequest.last_escalated_at.is_(None),
                    )
                    .with_for_update()
                    .one_or_none()
                )
                if review is None:
                    continue
                now = utc_now()
                review.escalation_count += 1
                review.last_escalated_at = now
                recipient = (
                    self.settings.escalation_alert_email_to.strip()
                    or self.settings.alert_email_to.strip()
                    or "unconfigured"
                )
                self.db.add(SupportAuditEvent(
                    review_id=review.id, event_type="escalated", actor="system",
                    payload_json=json.dumps({"status": review.status, "recipient": recipient}),
                ))
                existing_job = self.db.query(ToolJob).filter(
                    ToolJob.report_id == review.report_id,
                    ToolJob.kind == ToolJobKind.ESCALATION_ALERT.value,
                ).first()
                if existing_job is None:
                    self.db.add(ToolJob(
                        report_id=review.report_id, kind=ToolJobKind.ESCALATION_ALERT.value,
                        status=ToolJobStatus.PENDING.value, attempts=0,
                        max_attempts=self.settings.tool_queue_max_attempts,
                        run_after=now, last_error="",
                    ))
                escalated.append(review.id)
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        return escalated

    def enqueue_due_follow_up_reminders(self) -> list[int]:
        """为到期持续关注登记一次提醒任务，不把提醒写成现实跟进结果。"""
        due_ids = (
            self.db.query(ReviewRequest)
            .filter(
                ReviewRequest.status == "monitoring",
                ReviewRequest.follow_up_at.is_not(None),
                ReviewRequest.follow_up_at <= utc_now(),
            )
            .with_entities(ReviewRequest.id)
            .all()
        )
        reminded: list[int] = []
        for (review_id,) in due_ids:
            try:
                review = (
                    self.db.query(ReviewRequest)
                    .filter(ReviewRequest.id == review_id, ReviewRequest.status == "monitoring")
                    .with_for_update()
                    .one_or_none()
                )
                if review is None:
                    continue
                existing_job = self.db.query(ToolJob).filter(
                    ToolJob.report_id == review.report_id,
                    ToolJob.kind == ToolJobKind.FOLLOW_UP_REMINDER.value,
                ).first()
                if existing_job is not None:
                    self.db.commit()
                    continue
                now = utc_now()
                self.db.add(ToolJob(
                    report_id=review.report_id, kind=ToolJobKind.FOLLOW_UP_REMINDER.value,
                    status=ToolJobStatus.PENDING.value, attempts=0,
                    max_attempts=self.settings.tool_queue_max_attempts,
                    run_after=now, last_error="",
                ))
                self.db.add(SupportAuditEvent(
                    review_id=review.id, event_type="follow_up_reminder", actor="system",
                    payload_json=json.dumps({
                        "followUpAt": utc_isoformat(review.follow_up_at),
                        "owner": review.follow_up_owner,
                    }),
                ))
                reminded.append(review.id)
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        return reminded

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
        self, review_id: int, outcome: ReviewOutcome, reviewed_by: str,
    ) -> ReviewRequest:
        """原子保存处置结果、用户说明和审计，不允许恢复常规 AI 支持。"""
        try:
            review = self._get_in_progress(review_id, reviewed_by)
            review.reviewer_decision = outcome.decision
            review.reviewer_note = outcome.note
            review.reviewed_by = reviewed_by
            review.referral_target = outcome.referral_target
            review.next_step = outcome.next_step
            review.follow_up_owner = outcome.follow_up_owner
            review.follow_up_at = outcome.follow_up_at
            review.completion_basis = outcome.completion_basis
            review.reviewed_at = utc_now()
            review.status = outcome.status
            self._append_support_message_and_audit(
                review=review, actor=reviewed_by, event_type="outcome",
                content=self.support_outcome_message(review),
                audit_payload={"decision": outcome.decision, "status": review.status},
                fault_stage="outcome",
            )
            self.db.refresh(review)
            return review
        except Exception:
            self.db.rollback()
            raise

    def get_review(self, review_id: int) -> ReviewRequest | None:
        """按内部编号读取审核记录，缺失时返回 None。

        本服务方法不负责接口权限验证，也不改变审核状态。
        """
        return self.db.get(ReviewRequest, review_id)

    @staticmethod
    def support_outcome_message(review: ReviewRequest) -> str:
        """为三类人工处置生成用户可见说明，不表示现实服务已经完成。"""
        if review.reviewer_decision == "refer":
            return (
                f"支持人员记录了转介安排。去向：{review.referral_target}；下一步：{review.next_step}；"
                f"负责人：{review.follow_up_owner}。如你现在处于紧急危险中，请立即联系身边可信任的人或当地紧急服务。"
            )
        if review.reviewer_decision == "monitor":
            when = review.follow_up_at.strftime("%Y-%m-%d %H:%M UTC") if review.follow_up_at else "后续约定时间"
            return (
                f"支持人员记录了持续关注安排。负责人：{review.follow_up_owner}；跟进时间：{when}。"
                "提醒不代表人工已经完成跟进；如情况变化，请主动联系身边可信任的人或当地支持服务。"
            )
        if review.reviewer_decision == "complete":
            return (
                f"本次人工支持已完成处置记录。处理记录：{review.reviewer_note}；"
                f"完成依据：{review.completion_basis}。这不表示风险已经解除或现实服务已经完成。"
            )
        return ""

    def _append_support_message_and_audit(
        self, *, review: ReviewRequest, actor: str, event_type: str, content: str,
        fault_stage: str, client_request_id: str | None = None,
        audit_payload: dict | None = None,
    ) -> ChatMessage:
        """在同一事务追加人工消息与审计，并执行既定故障注入点。"""
        session = self.db.get(ChatSession, review.session_id)
        if session is None:
            raise ValueError(f"Session {review.session_id} not found")
        message = ChatMessage(
            user_id=session.user_id, session_id=session.id, review_id=review.id,
            role=MessageRole.SUPPORT.value, content=content, client_request_id=client_request_id,
        )
        session.touch()
        self.db.add_all([message, session])
        self.db.flush()
        self.db.add(SupportAuditEvent(
            review_id=review.id, event_type=event_type, actor=actor,
            payload_json=json.dumps(audit_payload or {"messageId": message.id}),
        ))
        self.fault_injector(fault_stage)
        self.db.commit()
        self.db.refresh(message)
        return message

    def _get_in_progress(self, review_id: int, actor: str) -> ReviewRequest:
        review = (
            self.db.query(ReviewRequest)
            .filter(ReviewRequest.id == review_id)
            .with_for_update()
            .one_or_none()
        )
        if review is None:
            raise ValueError(f"Review {review_id} not found")
        if review.status != "in_progress":
            raise ReviewConflictError(f"Review {review_id} is not in progress (status={review.status})")
        if review.claimed_by != actor:
            raise ReviewConflictError(f"Review {review_id} is owned by {review.claimed_by}")
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
            "completionBasis": review.completion_basis,
            "claimedBy": review.claimed_by,
            "claimedAt": utc_isoformat(review.claimed_at) if review.claimed_at else None,
            "escalationCount": review.escalation_count,
            "lastEscalatedAt": utc_isoformat(review.last_escalated_at) if review.last_escalated_at else None,
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
        """用独立会话执行一轮超时升级和到期持续关注提醒。"""
        db = self.session_factory()
        try:
            service = ReviewService(db, self.settings)
            return service.escalate_timed_out() + service.enqueue_due_follow_up_reminders()
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
