from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, make_url
from sqlalchemy.orm import sessionmaker

from app.agents.runtime import AgentRunResult
from app.core.config import Settings
from app.core.database import Base
from app.core.enums import EmotionLabel, IntentType, RiskLevel
from app.models.entities import (
    ChatMessage,
    ChatSession,
    OffsiteContactRecord,
    ReviewRequest,
    SafetyAssessmentRecord,
    SupportAuditEvent,
    ToolJob,
    UserAccount,
)
from app.services.assessment import PsychologyAssessment
from app.services.review import ReviewOutcome, ReviewService
from app.services.support_turn import SupportTurnTransaction

MYSQL_TEST_DATABASE_URL = os.getenv("MYSQL_TEST_DATABASE_URL", "")


@pytest.fixture
def mysql_db(mysql_test_database):
    """提供专用 MySQL 测试数据库，并在前后重建表结构。

    本机未显式配置时自动使用隔离容器；名称不以 _test 结尾则拒绝运行。
    """
    url = make_url(MYSQL_TEST_DATABASE_URL)
    if url.get_backend_name() != "mysql" or not (url.database or "").endswith("_test"):
        pytest.fail("MySQL 集成测试只允许使用名称以 _test 结尾的测试数据库")
    engine = create_engine(MYSQL_TEST_DATABASE_URL)
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = sessions()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _seed(db):
    """先提交测试用户，再准备尚未提交的新会话。

    让故障测试同时验证会话和本轮关联记录可以一起撤销。
    """
    user = UserAccount(username="mysql-student", display_name="学生", password_hash="hash")
    db.add(user)
    db.commit()
    session = ChatSession(public_id="mysql-turn", title="支持", user_id=user.id)
    db.add(session)
    db.flush()
    return user, session


def _high_risk_run() -> AgentRunResult:
    """构造待人工审核的高风险运行结果。

    采用固定评估字段隔离模型服务，专注验证数据库事务。
    """
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


@pytest.mark.parametrize("stage", ["message", "report", "review", "jobs"])
def test_mysql_fault_injection_rolls_back_the_whole_support_turn(mysql_db, stage):
    """在专用数据库的不同保存阶段抛出模拟异常。

    确认新会话、消息、评估、审核和工具任务都没有残留。
    """
    user, session = _seed(mysql_db)

    def fail(current_stage: str) -> None:
        """匹配本轮参数指定的阶段后抛错。

        其他阶段继续执行，确保故障发生在预期的保存位置。
        """
        if current_stage == stage:
            raise RuntimeError(f"fail after {stage}")

    with pytest.raises(RuntimeError, match="fail after"):
        SupportTurnTransaction(
            mysql_db,
            Settings(tool_queue_enabled=True),
            fault_injector=fail,
        ).save_support_turn(
            user=user,
            session=session,
            content="我不想活了",
            run=_high_risk_run(),
            handoff_reason="HIGH_RISK_KEYWORD",
        )

    assert mysql_db.query(ChatSession).count() == 0
    assert mysql_db.query(ChatMessage).count() == 0
    assert mysql_db.query(SafetyAssessmentRecord).count() == 0
    assert mysql_db.query(ReviewRequest).count() == 0
    assert mysql_db.query(ToolJob).count() == 0


def _seed_human_review(db):
    user, session = _seed(db)
    report = SafetyAssessmentRecord(
        user_id=user.id, session_id=session.id, content="我不想活了", intent="RISK",
        emotion="HIGH_RISK", emotion_score=4.0, risk_level="HIGH", confidence=0.95, summary="高风险",
    )
    db.add(report)
    db.flush()
    review = ReviewRequest(
        session_id=session.id, report_id=report.id, thread_id=session.public_id,
        risk_summary="高风险", status="pending",
    )
    db.add(review)
    db.commit()
    return user, session, review


def test_mysql_human_support_follow_up_rolls_back_both_messages(mysql_db):
    user, session, review = _seed_human_review(mysql_db)

    def fail(stage: str) -> None:
        if stage == "human_support_follow_up":
            raise RuntimeError("fail after follow up")

    with pytest.raises(RuntimeError, match="fail after follow up"):
        SupportTurnTransaction(mysql_db, Settings(), fault_injector=fail).save_human_support_follow_up(
            user=user, session=session, review=review, content="补充情况", acknowledgment="已收到",
        )

    assert mysql_db.query(ChatMessage).count() == 0
    assert mysql_db.get(ReviewRequest, review.id).status == "pending"


@pytest.mark.parametrize("stage", ["human_reply", "offsite_contact", "outcome"])
def test_mysql_human_action_and_audit_roll_back_together(mysql_db, stage):
    _, _, review = _seed_human_review(mysql_db)
    settings = Settings()
    ReviewService(mysql_db, settings).claim(review.id, "admin")

    def fail(current_stage: str) -> None:
        if current_stage == stage:
            raise RuntimeError(f"fail after {stage}")

    service = ReviewService(mysql_db, settings, fault_injector=fail)
    with pytest.raises(RuntimeError, match=f"fail after {stage}"):
        if stage == "human_reply":
            service.reply(review.id, "人工回复", "admin", "mysql-reply-1")
        elif stage == "offsite_contact":
            service.record_offsite_contact(
                review.id, contact_method="电话", contacted_at=datetime(2026, 10, 30, tzinfo=UTC),
                outcome="已联系", next_step="继续跟进", recorded_by="admin", client_request_id="mysql-contact-1",
            )
        else:
            service.mark_decision(
                review.id,
                ReviewOutcome(decision="complete", note="处理记录", completion_basis="完成依据"),
                "admin",
            )

    assert mysql_db.get(ReviewRequest, review.id).status == "in_progress"
    assert mysql_db.query(ChatMessage).count() == 0
    assert mysql_db.query(OffsiteContactRecord).count() == 0
    assert mysql_db.query(SupportAuditEvent).filter_by(review_id=review.id).count() == 1
    assert mysql_db.query(SupportAuditEvent).filter_by(review_id=review.id).one().event_type == "claimed"
