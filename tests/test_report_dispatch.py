from __future__ import annotations

import asyncio

import pytest

from app.core.config import Settings
from app.models.entities import ToolJob
from app.services.mcp_client import McpToolError
from app.services.report_dispatch import (
    McpReportDispatcher,
    QueueReportDispatcher,
    ReportDispatchError,
    create_report_dispatcher,
)
from app.services.tool_queue import ToolQueueService


def test_queue_dispatcher_enqueues_report(database_harness):
    db = database_harness.sessions()
    try:
        dispatcher = create_report_dispatcher(
            db,
            Settings(tool_queue_enabled=True),
        )
        assert isinstance(dispatcher, QueueReportDispatcher)
        asyncio.run(dispatcher.dispatch(42, "HIGH"))
        jobs = db.query(ToolJob).filter(ToolJob.report_id == 42).all()
        assert [job.kind for job in jobs] == ["EXCEL_REPORT", "RISK_ALERT"]
    finally:
        db.close()


def test_mcp_dispatcher_is_selected_when_queue_is_disabled():
    calls = []

    class Client:
        async def handle_report(self, report_id: int, risk_level: str | None):
            calls.append((report_id, risk_level))

    dispatcher = create_report_dispatcher(
        None,
        Settings(tool_queue_enabled=False),
    )
    assert isinstance(dispatcher, McpReportDispatcher)
    dispatcher.client = Client()
    asyncio.run(dispatcher.dispatch(7, "LOW"))
    assert calls == [(7, "LOW")]


def test_mcp_errors_are_exposed_as_dispatch_errors():
    class FailingClient:
        async def handle_report(self, report_id: int, risk_level: str | None):
            raise McpToolError("tool failed")

    dispatcher = McpReportDispatcher(Settings(tool_queue_enabled=False))
    dispatcher.client = FailingClient()
    with pytest.raises(ReportDispatchError, match="tool failed"):
        asyncio.run(dispatcher.dispatch(7, "HIGH"))


def test_only_one_worker_can_atomically_claim_a_pending_job(database_harness):
    first = database_harness.sessions()
    second = database_harness.sessions()
    try:
        settings = Settings(tool_queue_enabled=True)
        job = ToolQueueService(first, settings).enqueue_report(99, "LOW")[0]

        first_claim = ToolQueueService(first, settings).claim_job(job.id)
        second_claim = ToolQueueService(second, settings).claim_job(job.id)

        assert first_claim is True
        assert second_claim is False
    finally:
        first.close()
        second.close()
