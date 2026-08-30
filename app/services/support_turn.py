from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.agents.runtime import AgentRunResult
from app.core.config import Settings
from app.core.enums import MessageRole, RiskLevel, ToolJobKind, ToolJobStatus
from app.core.time import utc_now
from app.models.entities import (
    ChatMessage,
    ChatSession,
    PsychologicalReport,
    ReviewRequest,
    ToolJob,
    UserAccount,
)
from app.services.review import HANDOFF_REASONS


@dataclass(frozen=True)
class PersistedSupportTurn:
    message: ChatMessage
    report: PsychologicalReport | None
    review: ReviewRequest | None
    jobs: list[ToolJob]


class SupportTurnTransaction:
    """Persist the durable facts of one turn with one commit."""

    def __init__(
        self,
        db: Session,
        settings: Settings,
        *,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.fault_injector = fault_injector or (lambda stage: None)

    def save_support_turn(
        self,
        *,
        user: UserAccount,
        session: ChatSession,
        content: str,
        run: AgentRunResult,
        handoff_reason: str = "HIGH_RISK_KEYWORD",
        desensitized_summary: str = "",
    ) -> PersistedSupportTurn:
        report = None
        review = None
        jobs: list[ToolJob] = []
        try:
            message = ChatMessage(
                user_id=user.id,
                session_id=session.id,
                role=MessageRole.USER.value,
                content=content,
            )
            session.touch()
            self.db.add_all([message, session])
            self.db.flush()
            self.fault_injector("message")

            if run.requires_report and run.assessment is not None:
                assessment = run.assessment
                report = PsychologicalReport(
                    user_id=user.id,
                    session_id=session.id,
                    content=content,
                    intent=run.intent.value,
                    emotion=assessment.emotion.value,
                    emotion_score=assessment.emotion_score,
                    risk_level=run.risk_level.value,
                    confidence=assessment.confidence,
                    summary=assessment.summary,
                )
                self.db.add(report)
                self.db.flush()
                self.fault_injector("report")

                if run.pending_review:
                    if handoff_reason not in HANDOFF_REASONS:
                        raise ValueError(f"Invalid handoff reason: {handoff_reason}")
                    review = ReviewRequest(
                        session_id=session.id,
                        report_id=report.id,
                        thread_id=session.public_id,
                        risk_summary=assessment.summary,
                        handoff_reason=handoff_reason,
                        desensitized_summary=desensitized_summary,
                        status="pending",
                    )
                    self.db.add(review)
                    self.db.flush()
                self.fault_injector("review")

                if self.settings.tool_queue_enabled:
                    excel_job = self._job(report.id, ToolJobKind.EXCEL_REPORT.value)
                    self.db.add(excel_job)
                    self.db.flush()
                    jobs.append(excel_job)
                    if run.risk_level == RiskLevel.HIGH:
                        alert_job = self._job(
                            report.id,
                            ToolJobKind.RISK_ALERT.value,
                            depends_on_job_id=excel_job.id,
                        )
                        self.db.add(alert_job)
                        self.db.flush()
                        jobs.append(alert_job)
                self.fault_injector("jobs")

            self.db.commit()
            for row in [message, report, review, *jobs]:
                if row is not None:
                    self.db.refresh(row)
            return PersistedSupportTurn(message, report, review, jobs)
        except Exception:
            self.db.rollback()
            raise

    def _job(
        self,
        report_id: int,
        kind: str,
        *,
        depends_on_job_id: int | None = None,
    ) -> ToolJob:
        return ToolJob(
            report_id=report_id,
            kind=kind,
            status=ToolJobStatus.PENDING.value,
            attempts=0,
            max_attempts=self.settings.tool_queue_max_attempts,
            depends_on_job_id=depends_on_job_id,
            run_after=utc_now(),
            last_error="",
        )
