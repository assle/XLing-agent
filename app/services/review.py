"""Service for managing high-risk message review queue (issues 06-07, 11)."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import SessionLocal
from app.core.enums import MessageRole
from app.models.entities import ChatMessage, ChatSession, PsychologicalReport, ReviewRequest


_RISK_PRIORITY = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}

HANDOFF_REASONS = {
    "HIGH_RISK_KEYWORD", "RISK_TRAJECTORY_RISING",
    "SUSTAINED_NO_IMPROVEMENT", "USER_REQUEST", "TIMEOUT",
}

REVIEW_DECISIONS = {"approve", "reject", "refer", "monitor"}


class ReviewService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings

    def list_pending(self) -> list[dict]:
        """List all pending reviews, sorted by urgency: higher risk first,
        then longer wait time first."""
        reviews = (
            self.db.query(ReviewRequest)
            .filter(ReviewRequest.status == "pending")
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
        """All reviews: pending first (urgency order), then decided (newest first)."""
        reviews = self.db.query(ReviewRequest).all()
        items = [self._to_dict(r) for r in reviews]
        pending = [i for i in items if i.get("status") == "pending"]
        pending.sort(
            key=lambda x: (
                -_RISK_PRIORITY.get(x.get("riskLevel") or "", 0),
                -(x.get("waitSeconds") or 0),
            )
        )
        decided = [i for i in items if i.get("status") != "pending"]
        decided.sort(key=lambda x: x.get("reviewedAt") or "", reverse=True)
        return pending + decided

    def escalate_timed_out(self) -> list[int]:
        """Auto-send the fallback response for reviews pending longer than the
        configured timeout (issue 07: 15 min, action = auto-fallback).
        Returns the list of escalated review IDs."""
        from app.services.ai import PromptTemplates

        cutoff = datetime.utcnow() - timedelta(minutes=self.settings.review_timeout_minutes)
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
            if review is None:
                continue
            session = self.db.get(ChatSession, review.session_id)
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
            review.reviewed_at = datetime.utcnow()
            escalated.append(review.id)
            self.db.commit()
        return escalated

    async def resume_and_respond(self, review: ReviewRequest, approved: bool) -> tuple[str, bool]:
        """Resume the interrupted agent run and persist the student-facing message.

        Returns (response_text, degraded). Approve -> CounselorAgent generates the
        response; reject / degraded (checkpoint lost, e.g. after a restart) ->
        the fixed fallback is persisted instead.
        """
        from app.services.ai import AiClient, PromptTemplates

        session = self.db.get(ChatSession, review.session_id)
        if session is None:
            raise ValueError(f"Session {review.session_id} not found for review {review.id}")
        try:
            from app.agents.langgraph_runtime import LangGraphAgentRuntimeService
            runtime = LangGraphAgentRuntimeService(self.db, self.settings)
            result = await runtime.resume(review.thread_id, approved=approved)
        except ModuleNotFoundError:
            # Custom-runtime deployments cannot resume a checkpoint. Keep the
            # safety boundary by sending only the fixed fallback response.
            response_text = PromptTemplates.fallback_response()
            self.db.add(ChatMessage(
                user_id=session.user_id,
                session_id=review.session_id,
                role=MessageRole.ASSISTANT.value,
                content=response_text,
            ))
            self.db.commit()
            return response_text, True
        if result.degraded or not approved:
            response_text = result.fallback_response or PromptTemplates.fallback_response()
        else:
            tokens = [token async for token in AiClient(self.settings).stream(result.response_messages)]
            response_text = "".join(tokens)
        self.db.add(ChatMessage(
            user_id=session.user_id,
            session_id=review.session_id,
            role=MessageRole.ASSISTANT.value,
            content=response_text,
        ))
        self.db.commit()
        return response_text, result.degraded

    def mark_escalated(self, review_id: int) -> ReviewRequest:
        """Mark a review as auto-escalated (e.g. checkpoint lost after restart)."""
        review = self._get_pending(review_id)
        review.status = "escalated"
        review.reviewed_at = datetime.utcnow()
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
        """Create a review request with handoff reason and desensitized summary (issue 11)."""
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
        """Record reviewer decision with audit fields (issue 11)."""
        if decision not in REVIEW_DECISIONS:
            raise ValueError(f"Invalid decision: {decision}. Valid: {REVIEW_DECISIONS}")
        referral_target = referral_target.strip() if referral_target else None
        next_step = next_step.strip() if next_step else None
        follow_up_owner = follow_up_owner.strip() if follow_up_owner else None
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
        review.follow_up_at = follow_up_at
        review.reviewed_at = datetime.utcnow()
        # Map decision to status
        status_map = {"approve": "approved", "reject": "rejected", "refer": "referred", "monitor": "monitoring"}
        review.status = status_map[decision]
        self.db.commit()
        return review

    def get_review(self, review_id: int) -> ReviewRequest | None:
        return self.db.get(ReviewRequest, review_id)

    @staticmethod
    def student_message(review: ReviewRequest) -> str:
        """Build a student-facing message for decisions with a next action."""
        if review.reviewer_decision == "refer":
            return (
                f"人工审核建议你联系{review.referral_target}。"
                f"下一步：{review.next_step}。如你现在处于紧急危险中，请立即联系身边可信任的人或当地紧急服务。"
            )
        if review.reviewer_decision == "monitor":
            when = review.follow_up_at.strftime("%Y-%m-%d %H:%M") if review.follow_up_at else "后续约定时间"
            return f"人工审核记录了持续关注建议。负责人：{review.follow_up_owner}；建议跟进时间：{when}。如情况变化，请主动联系身边可信任的人或当地支持服务。"
        return ""

    def persist_student_message(self, review: ReviewRequest) -> str:
        """Persist and return the next-action message for the student, if any."""
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
        review = self.db.get(ReviewRequest, review_id)
        if review is None:
            raise ValueError(f"Review {review_id} not found")
        if review.status != "pending":
            raise ValueError(f"Review {review_id} is not pending (status={review.status})")
        return review

    def _to_dict(self, review: ReviewRequest) -> dict:
        report = self.db.get(PsychologicalReport, review.report_id)
        messages = (
            self.db.query(ChatMessage)
            .filter(ChatMessage.session_id == review.session_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(10)
            .all()
        )
        messages.reverse()
        now = datetime.utcnow()
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
            "followUpAt": review.follow_up_at.isoformat() if review.follow_up_at else None,
            "status": review.status,
            "reviewedAt": review.reviewed_at.isoformat() if review.reviewed_at else None,
            "riskLevel": report.risk_level if report else None,
            "emotion": report.emotion if report else None,
            "emotionScore": report.emotion_score if report else None,
            "studentMessage": report.content if report else None,
            "recentContext": [{"role": m.role, "content": m.content} for m in messages],
            "createdAt": review.created_at.isoformat() if review.created_at else None,
            "waitSeconds": (now - review.created_at).total_seconds() if review.created_at else None,
        }


class ReviewTimeoutWorker:
    """Run review timeout handling independently of the admin dashboard."""

    def __init__(self, settings: Settings, session_factory: Callable[[], Session] = SessionLocal):
        self.settings = settings
        self.session_factory = session_factory
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread is not None:
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="xling-review-timeouts", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None

    def run_once(self) -> list[int]:
        db = self.session_factory()
        try:
            return ReviewService(db, self.settings).escalate_timed_out()
        finally:
            db.close()

    def _loop(self) -> None:
        interval = max(1.0, self.settings.review_timeout_poll_interval_seconds)
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                # The next interval retries; no student content is logged here.
                logging.getLogger(__name__).exception("Review timeout worker failed")
            self.stop_event.wait(interval)


_timeout_worker: ReviewTimeoutWorker | None = None


def get_review_timeout_worker(settings: Settings) -> ReviewTimeoutWorker:
    global _timeout_worker
    if _timeout_worker is None:
        _timeout_worker = ReviewTimeoutWorker(settings)
    return _timeout_worker
