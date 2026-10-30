from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.core.bootstrap import create_schema
from app.core.config import Settings
from app.services.knowledge import KnowledgeService
from tests.support import DatabaseHarness


def test_create_schema_adds_tool_job_uniqueness_to_existing_database():
    """先创建缺少唯一约束的旧任务表，再运行结构初始化。

    重复插入同记录同类型任务必须触发完整性错误，验证约束确实生效。
    """
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text(
            """
            CREATE TABLE tool_jobs (
                id INTEGER PRIMARY KEY,
                report_id INTEGER NOT NULL,
                kind VARCHAR(64) NOT NULL
            )
            """
        ))

    create_schema(engine)

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO tool_jobs(report_id, kind) VALUES (1, 'EXCEL_REPORT')"
            ))
            connection.execute(text(
                "INSERT INTO tool_jobs(report_id, kind) VALUES (1, 'EXCEL_REPORT')"
            ))


def test_create_schema_upgrades_human_support_columns_and_uniqueness():
    """旧库启动后应获得人工处置字段、审计关联和请求编号唯一约束。"""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE review_requests (
                id INTEGER PRIMARY KEY,
                session_id INTEGER NOT NULL,
                report_id INTEGER NOT NULL,
                thread_id VARCHAR(64) NOT NULL,
                risk_summary TEXT NOT NULL,
                handoff_reason VARCHAR(64),
                desensitized_summary TEXT NOT NULL,
                reviewer_decision VARCHAR(32),
                reviewer_note TEXT NOT NULL,
                reviewed_by VARCHAR(64),
                referral_target VARCHAR(200),
                next_step VARCHAR(500),
                follow_up_owner VARCHAR(128),
                follow_up_at DATETIME,
                status VARCHAR(32) NOT NULL,
                created_at DATETIME NOT NULL,
                reviewed_at DATETIME
            )
        """))
        connection.execute(text("""
            CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                session_id INTEGER NOT NULL,
                role VARCHAR(32) NOT NULL,
                content TEXT NOT NULL,
                created_at DATETIME NOT NULL
            )
        """))
        connection.execute(text("""
            CREATE TABLE alert_records (
                id INTEGER PRIMARY KEY,
                report_id INTEGER NOT NULL,
                channel VARCHAR(64) NOT NULL,
                recipient VARCHAR(256) NOT NULL,
                status VARCHAR(32) NOT NULL,
                message TEXT NOT NULL,
                created_at DATETIME NOT NULL
            )
        """))
        connection.execute(text("""
            CREATE TABLE offsite_contact_records (
                id INTEGER PRIMARY KEY,
                review_id INTEGER NOT NULL,
                contact_method VARCHAR(32) NOT NULL,
                contacted_at DATETIME NOT NULL,
                outcome TEXT NOT NULL,
                next_step TEXT NOT NULL,
                client_request_id VARCHAR(64),
                recorded_by VARCHAR(64) NOT NULL,
                created_at DATETIME NOT NULL
            )
        """))
        connection.execute(text(
            "INSERT INTO review_requests(id, session_id, report_id, thread_id, risk_summary, "
            "desensitized_summary, reviewer_note, status, created_at) "
            "VALUES (1, 1, 1, 'old', '', '', '', 'pending', CURRENT_TIMESTAMP)"
        ))
        connection.execute(text(
            "INSERT INTO alert_records(id, report_id, channel, recipient, status, message, created_at) "
            "VALUES (1, 1, 'email', 'old', 'SUCCESS', '', CURRENT_TIMESTAMP)"
        ))

    create_schema(engine)

    inspector = inspect(engine)
    review_columns = {column["name"] for column in inspector.get_columns("review_requests")}
    assert {
        "completion_basis", "claimed_by", "claimed_at", "escalation_count", "last_escalated_at",
    } <= review_columns
    assert {"review_id", "client_request_id"} <= {
        column["name"] for column in inspector.get_columns("chat_messages")
    }
    assert "notification_kind" in {column["name"] for column in inspector.get_columns("alert_records")}
    with engine.connect() as connection:
        assert connection.execute(text("SELECT escalation_count FROM review_requests WHERE id = 1")).scalar() == 0
        assert connection.execute(text("SELECT notification_kind FROM alert_records WHERE id = 1")).scalar() == "RISK_ALERT"

    for table, expected_columns in (
        ("chat_messages", {"review_id", "role", "client_request_id"}),
        ("offsite_contact_records", {"review_id", "client_request_id"}),
    ):
        constraints = [*inspector.get_unique_constraints(table), *inspector.get_indexes(table)]
        assert any(
            item.get("unique", True) and set(item.get("column_names") or []) == expected_columns
            for item in constraints
        ), table


def test_ensure_source_repairs_an_incomplete_vector_index():
    """先导入知识，再让替身索引报告零条内容。

    再次检查同来源时仍应调用同步，不能只因数据库正文未变而跳过修复。
    """
    harness = DatabaseHarness()
    db = harness.sessions()
    settings = Settings(
        _env_file=None,
        ai_provider="mock",
        knowledge_vector_enabled=False,
    )
    service = KnowledgeService(db, settings)
    content = "一段用于验证知识向量索引自愈的内容。"

    try:
        service.ensure_source("recovery-test.md", content)

        class IncompleteVectorStore:
            can_embed = True

            @staticmethod
            def count() -> int:
                """固定返回零，模拟索引条目缺失。

                配合同步调用记录检查知识服务的恢复路径。
                """
                return 0

        service.vector_store = IncompleteVectorStore()
        synchronized = []
        # 用匿名函数收集需要同步的数据库行，避免实际访问向量服务。
        service._sync_vector_chunks = lambda rows: synchronized.extend(rows)

        service.ensure_source("recovery-test.md", content)

        assert [row.source for row in synchronized] == ["recovery-test.md"]
    finally:
        db.close()
        harness.close()
