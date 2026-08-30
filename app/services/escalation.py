"""Risk-triggered closed-loop escalation (issue 12).

Connects risk trajectory, screening, next-day feedback, and human review into
a complete safety-escalation path:

  进入 -> 消息分流 -> 安全风险评估 -> 认知行为四维追问 -> 行动计划 -> 次日反馈 -> 安全升级或完成

Escalation triggers:
  - RISK_TRAJECTORY_RISING: trajectory rising past threshold
  - SUSTAINED_NO_IMPROVEMENT: next-day feedback reports worsening or no improvement
  - HIGH_RISK_KEYWORD: high-risk screening answers
  - USER_REQUEST: user explicitly requests human support
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from app.core.enums import RiskLevel
from app.services.privacy import PrivacySanitizer
from app.services.review import ReviewService

logger = logging.getLogger(__name__)


@dataclass
class EscalationResult:
    """Result of an escalation check."""
    should_escalate: bool
    handoff_reason: Optional[str] = None
    desensitized_summary: str = ""
    user_message: str = ""
    review_id: Optional[int] = None


# Safety message shown to user before any escalation
SAFETY_MESSAGE = (
    "我注意到你可能需要更多支持。你的情况已进入人工审核流程。"
    "如果你现在处于紧急情况，请立刻联系身边可信任的人、"
    "学校心理中心，或拨打 24 小时心理援助热线 400-161-9995。"
)

# Screening suggestion message (not forced)
SCREENING_SUGGESTION = (
    "我注意到你最近的情绪有一些变化。如果你愿意，可以完成一次简短的自愿量表筛查（PHQ-9 或 GAD-7），"
    "这可以帮助你更好地了解自己的状态。这只是自愿的筛查，不会影响你和我的对话。"
)


class EscalationService:
    """Orchestrates risk-triggered escalation across services (issue 12)."""

    def __init__(self, db: Session, review_svc: ReviewService | None = None):
        self.db = db
        self.review_svc = review_svc

    def check_trajectory_escalation(
        self,
        user_id: int,
        session_id: int | None,
        report_id: int,
        thread_id: str,
        current_risk: RiskLevel,
        trajectory_rising: bool,
        current_difficulty: str = "",
        risk_trend: str = "",
    ) -> EscalationResult:
        """Check if rising trajectory should trigger escalation.

        Single message not HIGH but trajectory rising -> RISK_TRAJECTORY_RISING review.
        Explicit HIGH is already handled by keyword path.
        """
        if current_risk == RiskLevel.HIGH:
            # HIGH risk keyword path handles this separately
            return EscalationResult(should_escalate=False)

        if not trajectory_rising:
            return EscalationResult(should_escalate=False)

        summary = PrivacySanitizer.build_review_summary(
            current_difficulty=current_difficulty,
            risk_trend=risk_trend or "风险轨迹连续上升",
        )
        return self._create_escalation(
            user_id, session_id, report_id, thread_id,
            handoff_reason="RISK_TRAJECTORY_RISING",
            desensitized_summary=summary,
        )

    def check_checkin_escalation(
        self,
        user_id: int,
        session_id: int | None,
        report_id: int | None,
        thread_id: str,
        improvement_status: str,
        current_difficulty: str = "",
    ) -> EscalationResult:
        """Check if next-day feedback should trigger safety escalation.

        Worsened or sustained no improvement -> SUSTAINED_NO_IMPROVEMENT review.
        report_id may be None: next-day feedback has no prior assessment report, so an
        honestly-labeled one is created for review-queue display.
        """
        if improvement_status not in ("worsened", "unchanged"):
            return EscalationResult(should_escalate=False)

        status_label = "情况恶化" if improvement_status == "worsened" else "没有改善"
        summary = PrivacySanitizer.build_review_summary(
            current_difficulty=current_difficulty or f"次日反馈：{status_label}",
            risk_trend=f"次日反馈：{status_label}",
            action_plan_status="行动计划仍在进行，未见改善",
        )
        return self._create_escalation(
            user_id, session_id, report_id, thread_id,
            handoff_reason="SUSTAINED_NO_IMPROVEMENT",
            desensitized_summary=summary,
            report_kind="次日反馈",
        )

    def check_screening_escalation(
        self,
        user_id: int,
        session_id: int | None,
        report_id: int,
        thread_id: str,
        high_risk_flagged: bool,
        current_difficulty: str = "",
    ) -> EscalationResult:
        """Check if screening high-risk answers should trigger escalation."""
        if not high_risk_flagged:
            return EscalationResult(should_escalate=False)

        summary = PrivacySanitizer.build_review_summary(
            current_difficulty=current_difficulty,
            risk_trend="自愿量表筛查：检测到高风险答案",
        )
        return self._create_escalation(
            user_id, session_id, report_id, thread_id,
            handoff_reason="HIGH_RISK_KEYWORD",
            desensitized_summary=summary,
        )

    def user_request_escalation(
        self,
        user_id: int,
        session_id: int | None,
        report_id: int,
        thread_id: str,
        current_difficulty: str = "",
    ) -> EscalationResult:
        """User explicitly requests human support -> USER_REQUEST review."""
        summary = PrivacySanitizer.build_review_summary(
            current_difficulty=current_difficulty,
            risk_trend="学生主动请求人工支持",
        )
        return self._create_escalation(
            user_id, session_id, report_id, thread_id,
            handoff_reason="USER_REQUEST",
            desensitized_summary=summary,
        )

    @staticmethod
    def get_screening_suggestion() -> str:
        """Return the voluntary screening suggestion message."""
        return SCREENING_SUGGESTION

    @staticmethod
    def get_safety_message() -> str:
        """Return the safety message shown before escalation."""
        return SAFETY_MESSAGE

    def _create_escalation(
        self,
        user_id: int,
        session_id: int | None,
        report_id: int | None,
        thread_id: str,
        handoff_reason: str,
        desensitized_summary: str,
        report_kind: str = "对话",
    ) -> EscalationResult:
        """Create a review request with the given handoff reason.

        Review creation is skipped (with a warning) when there is no session to
        attach to: ReviewRequest.session_id / thread_id are required and the
        review queue renders from the linked session's context.
        """
        if self.review_svc is None:
            return EscalationResult(
                should_escalate=True,
                handoff_reason=handoff_reason,
                desensitized_summary=desensitized_summary,
                user_message=SAFETY_MESSAGE,
            )

        if session_id is None or not thread_id:
            logger.warning(
                "escalation (%s) for user %s has no session/thread; review not created",
                handoff_reason, user_id,
            )
            return EscalationResult(
                should_escalate=True,
                handoff_reason=handoff_reason,
                desensitized_summary=desensitized_summary,
                user_message=SAFETY_MESSAGE,
            )

        if report_id is None:
            report_id = self._ensure_report(user_id, session_id, handoff_reason, report_kind)

        review = self.review_svc.create_with_context(
            session_id=session_id,
            report_id=report_id,
            thread_id=thread_id,
            handoff_reason=handoff_reason,
            desensitized_summary=desensitized_summary,
        )
        return EscalationResult(
            should_escalate=True,
            handoff_reason=handoff_reason,
            desensitized_summary=desensitized_summary,
            user_message=SAFETY_MESSAGE,
            review_id=review.id,
        )

    def _ensure_report(self, user_id: int, session_id: int, handoff_reason: str, report_kind: str) -> int:
        """Create a minimal PsychologicalReport for non-conversation triggers.

        The review queue renders studentMessage/riskLevel from the linked report,
        so safety escalations that did not originate from a chat message
        need one. It is labeled honestly: not an assessment, confidence 0.
        """
        from app.models.entities import PsychologicalReport

        report = PsychologicalReport(
            user_id=user_id,
            session_id=session_id,
            content=handoff_reason,
            intent="CONSULT",
            emotion="NORMAL",
            emotion_score=0.0,
            risk_level="MEDIUM",
            confidence=0.0,
            summary=f"由{report_kind}触发的升级（非对话/量表评估）",
        )
        self.db.add(report)
        self.db.commit()
        self.db.refresh(report)
        return report.id
