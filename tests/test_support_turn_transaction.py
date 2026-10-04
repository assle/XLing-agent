from __future__ import annotations

import asyncio

import pytest

from app.agents.langgraph_runtime import LangGraphAgentRuntimeService
from app.agents.runtime import AgentRunResult
from app.core.config import Settings
from app.core.enums import EmotionLabel, IntentType, RiskLevel
from app.models.entities import (
    ChatMessage,
    ChatSession,
    ReviewRequest,
    SafetyAssessmentRecord,
    ToolJob,
    UserAccount,
)
from app.schemas.dtos import AiMessage
from app.services.assessment import PsychologyAssessment
from app.services.chat import ChatService
from app.services.support_turn import SupportTurnTransaction
from tests.support import DatabaseHarness, build_runtime


def _seed(db):
    """在传入测试数据库中保存一个用户和会话。

    返回已提交对象，使后续故障只影响本轮新记录。
    """
    user = UserAccount(username="student", display_name="学生", password_hash="hash")
    session = ChatSession(public_id="turn-session", title="支持", user_id=1)
    db.add_all([user, session])
    db.commit()
    return user, session


def _high_risk_run() -> AgentRunResult:
    """构造需要人工审核的高风险执行结果。

    包含固定评估和空回复，用于测试保存流程而不调用模型。
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


def test_support_turn_persists_message_report_review_and_jobs_atomically():
    """保存一轮高风险消息并启用工具队列。

    检查消息、评估、审核和两类任务均存在且数量正确，最后清理独立数据库。
    """
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
        assert db.query(SafetyAssessmentRecord).count() == 1
        assert db.query(ReviewRequest).count() == 1
        assert db.query(ToolJob).count() == 2
    finally:
        db.close()
        harness.close()


def test_new_session_is_not_committed_before_the_support_turn():
    """只调用会话定位创建新会话，随后主动回滚。

    检查取得编号不代表已提交，新会话记录应消失。
    """
    harness = DatabaseHarness()
    db = harness.sessions()
    try:
        user = UserAccount(username="student", display_name="学生", password_hash="hash")
        db.add(user)
        db.commit()
        service = object.__new__(ChatService)
        service.db = db

        session = service.resolve_session(user, None, "最近压力很大", None)
        assert session.id is not None
        db.rollback()

        assert db.query(ChatSession).count() == 0
    finally:
        db.close()
        harness.close()


def test_empty_redis_memory_is_rebuilt_from_committed_database_messages():
    """先保存一条用户消息，再让模拟缓存返回空历史。

    检查执行器从数据库恢复消息并调用缓存替换，模型上下文也包含该消息。
    """
    class FailedRedisMemory:
        def __init__(self):
            """准备记录缓存替换结果的列表。

            用于观察执行器是否真正执行了数据库恢复后的回填。
            """
            self.replaced = []

        def load_recent(self, session_public_id: str):
            """始终返回空历史，模拟缓存没有本会话内容。

            促使执行器尝试数据库恢复路径。
            """
            return []

        def messages_from_rows(self, rows):
            """将测试数据库消息转换成模型消息。

            保留正文并规范角色大小写，供恢复断言核对。
            """
            return [AiMessage(role=row.role.lower(), content=row.content) for row in rows]

        def replace(self, session_public_id: str, messages):
            """记录收到的消息列表副本。

            不会连接缓存服务，仅供测试检查回填内容。
            """
            self.replaced = list(messages)

    harness = DatabaseHarness()
    db = harness.sessions()
    try:
        user, session = _seed(db)
        SupportTurnTransaction(db, Settings(tool_queue_enabled=False)).save_support_turn(
            user=user,
            session=session,
            content="最近压力很大",
            run=AgentRunResult(
                intent=IntentType.CONSULT,
                risk_level=RiskLevel.LOW,
                assessment=PsychologyAssessment(
                    EmotionLabel.ANXIETY,
                    2.0,
                    RiskLevel.LOW,
                    0.8,
                    "压力表达",
                ),
                retrieved_knowledge=[],
                response_messages=[],
                steps=[],
            ),
        )
        memory = FailedRedisMemory()
        runtime = build_runtime(
            LangGraphAgentRuntimeService,
            db=db,
            memory=memory,
            settings=Settings(ai_provider="mock", langgraph_checkpoint_backend="memory", knowledge_vector_enabled=False),
        )
        result = asyncio.run(runtime.run(user, session, "帮我写代码"))

        assert [message.content for message in memory.replaced] == ["最近压力很大"]
        assert any(message.content == "最近压力很大" for message in result.response_messages)
    finally:
        db.close()
        harness.close()


@pytest.mark.parametrize("stage", ["message", "report", "review", "jobs"])
def test_support_turn_rolls_back_every_business_record_after_injected_failure(stage):
    """分别在消息、评估、审核和任务阶段模拟异常。

    检查各类本轮新记录全部撤销，避免出现只保存了一部分的支持过程。
    """
    harness = DatabaseHarness()
    db = harness.sessions()
    try:
        user, session = _seed(db)

        def fail(current_stage: str) -> None:
            """仅在当前阶段与外层参数一致时主动抛出异常。

            通过参数化测试覆盖保存流程的不同失败位置。
            """
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
        assert db.query(SafetyAssessmentRecord).count() == 0
        assert db.query(ReviewRequest).count() == 0
        assert db.query(ToolJob).count() == 0
    finally:
        db.close()
        harness.close()
