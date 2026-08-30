from __future__ import annotations

import os

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
    PsychologicalReport,
    ReviewRequest,
    ToolJob,
    UserAccount,
)
from app.services.assessment import PsychologyAssessment
from app.services.support_turn import SupportTurnTransaction

MYSQL_TEST_DATABASE_URL = os.getenv("MYSQL_TEST_DATABASE_URL", "")


@pytest.fixture
def mysql_db():
    if not MYSQL_TEST_DATABASE_URL:
        pytest.skip("需要 MYSQL_TEST_DATABASE_URL 才运行 MySQL 事务集成测试")
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
    user = UserAccount(username="mysql-student", display_name="学生", password_hash="hash")
    db.add(user)
    db.commit()
    session = ChatSession(public_id="mysql-turn", title="支持", user_id=user.id)
    db.add(session)
    db.flush()
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


@pytest.mark.parametrize("stage", ["message", "report", "review", "jobs"])
def test_mysql_fault_injection_rolls_back_the_whole_support_turn(mysql_db, stage):
    user, session = _seed(mysql_db)

    def fail(current_stage: str) -> None:
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
    assert mysql_db.query(PsychologicalReport).count() == 0
    assert mysql_db.query(ReviewRequest).count() == 0
    assert mysql_db.query(ToolJob).count() == 0
