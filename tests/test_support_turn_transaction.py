from __future__ import annotations

import pytest

from app.agents.runtime import AgentRunResult
from app.core.config import Settings
from app.core.enums import EmotionLabel, IntentType, RiskLevel
from app.models.entities import (
    ChatMessage,
    ChatSession,
    PsychologicalReport,
    ReviewRequest,
    ToolJob,
    UserAccount,
)
from app.services.assessment import PsychologyAssessment
from app.services.support_turn import SupportTurnTransaction
from tests.support import DatabaseHarness


def _seed(db):
    user = UserAccount(username="student", display_name="学生", password_hash="hash")
    session = ChatSession(public_id="turn-session", title="支持", user_id=1)
    db.add_all([user, session])
    db.commit()
    return user, session


def _high_risk_run() -> AgentRunResult:
    assessment = PsychologyAssessment(
        EmotionLabel.HIGH_RISK,
        4.0,
        RiskLevel.HIGH,
        0.95,
        "明确高风险表达",
    )
    return AgentRunResult(
        intent=IntentType.RISK,
        risk_level=RiskLevel.HIGH,
        assessment=assessment,
        retrieved_knowledge=[],
        response_messages=[],
        steps=[],
        pending_review=True,
    )


def test_support_turn_persists_message_report_review_and_jobs_atomically():
    harness = DatabaseHarness()
    db = harness.sessions()
    try:
        user, session = _seed(db)
        persisted = SupportTurnTransaction(db, Settings(tool_queue_enabled=True)).save_support_turn(
            user=user,
            session=session,
            content="我不想活了",
            run=_high_risk_run(),
            handoff_reason="HIGH_RISK_KEYWORD",
            desensitized_summary="当前困境：[已脱敏]",
        )

        assert persisted.message.id is not None
        assert persisted.report is not None
        assert persisted.review is not None
        assert [job.kind for job in persisted.jobs] == ["EXCEL_REPORT", "RISK_ALERT"]
        assert db.query(ChatMessage).count() == 1
        assert db.query(PsychologicalReport).count() == 1
        assert db.query(ReviewRequest).count() == 1
        assert db.query(ToolJob).count() == 2
    finally:
        db.close()
        harness.close()


@pytest.mark.parametrize("stage", ["message", "report", "review", "jobs"])
def test_support_turn_rolls_back_every_business_record_after_injected_failure(stage):
    harness = DatabaseHarness()
    db = harness.sessions()
    try:
        user, session = _seed(db)

        def fail(current_stage: str) -> None:
            if current_stage == stage:
                raise RuntimeError(f"fail after {stage}")

        with pytest.raises(RuntimeError, match="fail after"):
            SupportTurnTransaction(
                db,
                Settings(tool_queue_enabled=True),
                fault_injector=fail,
            ).save_support_turn(
                user=user,
                session=session,
                content="我不想活了",
                run=_high_risk_run(),
                handoff_reason="HIGH_RISK_KEYWORD",
                desensitized_summary="当前困境：[已脱敏]",
            )

        assert db.query(ChatMessage).count() == 0
        assert db.query(PsychologicalReport).count() == 0
        assert db.query(ReviewRequest).count() == 0
        assert db.query(ToolJob).count() == 0
    finally:
        db.close()
        harness.close()
