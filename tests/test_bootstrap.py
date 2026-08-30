from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app.core.bootstrap import create_schema


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
