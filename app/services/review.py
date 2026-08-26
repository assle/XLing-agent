"""Service for managing high-risk message review queue (issues 06-07, 11)."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import Settings
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
        timed_out = (
            self.db.query(ReviewRequest)
            .filter(ReviewRequest.status == "pending")
            .filter(ReviewRequest.created_at < cutoff)
            .all()
        )
        escalated: list[int] = []
        fallback = PromptTemplates.fallback_response()
        for review in timed_out:
            session = self.db.get(ChatSession, review.session_id)
            if session is not None:
                self.db.add(ChatMessage(
                    user_id=session.user_id,
                    session_id=review.session_id,
                    role=MessageRole.ASSISTANT.value,
                    content=fallback,
                ))
            review.status = "escalated"
            review.reviewed_at = datetime.utcnow()
            escalated.append(review.id)
        if escalated:
            self.db.commit()
        return escalated

    async def resume_and_respond(self, review: ReviewRequest, approved: bool) -> tuple[str, bool]:
        """Resume the interrupted agent run and persist the student-facing message.

        Returns (response_text, degraded). Approve -> CounselorAgent generates the
        response; reject / degraded (checkpoint lost, e.g. after a restart) ->
        the fixed fallback is persisted instead.
        """
        from app.agents.langgraph_runtime import LangGraphAgentRuntimeService
        from app.services.ai import AiClient, PromptTemplates

        runtime = LangGraphAgentRuntimeService(self.db, self.settings)
        result = await runtime.resume(review.thread_id, approved=approved)
        session = self.db.get(ChatSession, review.session_id)
        if session is None:
            raise ValueError(f"Session {review.session_id} not found for review {review.id}")
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
        self, review_id: int, decision: str, note: str = "", reviewed_by: str = ""
    ) -> ReviewRequest:
        """Record reviewer decision with audit fields (issue 11)."""
        if decision not in REVIEW_DECISIONS:
            raise ValueError(f"Invalid decision: {decision}. Valid: {REVIEW_DECISIONS}")
        review = self._get_pending(review_id)
        review.reviewer_decision = decision
        review.reviewer_note = note
        review.reviewed_by = reviewed_by
        review.reviewed_at = datetime.utcnow()
        # Map decision to status
        status_map = {"approve": "approved", "reject": "rejected", "refer": "referred", "monitor": "monitoring"}
        review.status = status_map[decision]
        self.db.commit()
        return review

    def get_review(self, review_id: int) -> ReviewRequest | None:
        return self.db.get(ReviewRequest, review_id)

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
