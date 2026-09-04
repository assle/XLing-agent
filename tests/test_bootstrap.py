from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app.core.bootstrap import create_schema
from app.core.config import Settings
from app.services.knowledge import KnowledgeService
from tests.support import DatabaseHarness


def test_create_schema_adds_tool_job_uniqueness_to_existing_database():
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


def test_ensure_source_repairs_an_incomplete_vector_index():
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
                return 0

        service.vector_store = IncompleteVectorStore()
        synchronized = []
        service._sync_vector_chunks = lambda rows: synchronized.extend(rows)

        service.ensure_source("recovery-test.md", content)

        assert [row.source for row in synchronized] == ["recovery-test.md"]
    finally:
        db.close()
        harness.close()
