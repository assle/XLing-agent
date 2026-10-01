from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta

import pytest

from app.core import diagnostics
from app.core.config import Settings
from app.core.enums import ToolJobStatus
from app.core.time import utc_now
from app.models.entities import ToolJob
from app.services.mcp_client import McpToolError
from app.services.report_dispatch import (
    McpReportDispatcher,
    QueueReportDispatcher,
    ReportDispatchError,
    create_report_dispatcher,
)
from app.services.tool_queue import ToolQueueService, ToolQueueWorker


def test_queue_dispatcher_enqueues_report(database_harness):
    """启用队列后为高风险评估调度后处理。

    检查选中队列实现且登记表格和通知两种任务，不实际执行工具。
    """
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
    """关闭队列并注入记录调用的工具客户端。

    检查选择工具调用实现，记录编号和风险值正确传递。
    """
    calls = []

    class Client:
        async def handle_report(self, report_id: int, risk_level: str | None):
            """把收到的记录编号和风险值追加到外层列表。

            模拟正常工具处理，不启动子进程。
            """
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
    """让工具客户端抛出约定错误。

    检查调度器转成报告处理错误并保留原因文字。
    """
    class FailingClient:
        async def handle_report(self, report_id: int, risk_level: str | None):
            """模拟工具调用失败。

            供测试检查异常在调度接口边界的转换。
            """
            raise McpToolError("tool failed")

    dispatcher = McpReportDispatcher(Settings(tool_queue_enabled=False))
    dispatcher.client = FailingClient()
    with pytest.raises(ReportDispatchError, match="tool failed"):
        asyncio.run(dispatcher.dispatch(7, "HIGH"))


def test_only_one_worker_can_atomically_claim_a_pending_job(database_harness):
    """用两个独立会话依次领取同一个待处理任务。

    检查首次成功、第二次失败，证明条件更新防止重复领取该状态。
    """
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


def test_worker_recovery_only_requeues_expired_running_jobs(database_harness):
    """准备一个刚执行的任务和一个超过租约的任务。

    检查只恢复过期任务，仍有效的任务继续保持执行中。
    """
    db = database_harness.sessions()
    try:
        settings = Settings(tool_queue_enabled=True, tool_queue_lease_seconds=60)
        service = ToolQueueService(db, settings)
        current, stale = service.enqueue_report(100, "HIGH")
        current.status = ToolJobStatus.RUNNING.value
        current.updated_at = utc_now()
        stale.status = ToolJobStatus.RUNNING.value
        stale.updated_at = utc_now() - timedelta(minutes=2)
        db.commit()

        recovered = service.recover_expired_jobs()
        db.expire_all()

        assert recovered == 1
        assert db.get(ToolJob, current.id).status == ToolJobStatus.RUNNING.value
        assert db.get(ToolJob, stale.id).status == ToolJobStatus.PENDING.value
    finally:
        db.close()


def test_idempotent_registration_is_distinct_from_task_execution(database_harness, tmp_path):
    settings = Settings(diagnostic_log_dir=str(tmp_path))
    diagnostics.configure(settings)
    db = database_harness.sessions()
    try:
        with diagnostics.execution("chat") as trace:
            first = ToolQueueService(db, settings).enqueue_report(909, "LOW")
            second = ToolQueueService(db, settings).enqueue_report(909, "LOW")
        assert first[0].id == second[0].id
    finally:
        db.close()
        diagnostics.shutdown()
    records = [json.loads(line) for line in (tmp_path / "execution.jsonl").read_text().splitlines()]
    registrations = [row for row in records if row["event"] == "tool_job.registered"]
    assert [row["reused"] for row in registrations] == [False, True]
    assert {row["run_id"] for row in registrations} == {trace.run_id}
    assert not any(row["event"].startswith("tool_job.attempt") for row in records)


def test_background_failure_has_independent_runs_attempts_retry_and_final_failure(
    database_harness, tmp_path, monkeypatch,
):
    from app.services import tool_queue

    settings = Settings(
        diagnostic_log_dir=str(tmp_path), tool_queue_max_attempts=2,
        tool_queue_poll_interval_seconds=0.01, tool_queue_retry_delay_seconds=0,
    )
    diagnostics.configure(settings)
    monkeypatch.setattr(tool_queue, "SessionLocal", database_harness.sessions)
    db = database_harness.sessions()
    try:
        with diagnostics.execution("chat") as trace:
            job = ToolQueueService(db, settings).enqueue_report(909, "LOW")[0]
            job_id = job.id
    finally:
        db.close()
    worker = ToolQueueWorker(settings)
    try:
        worker.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            records = [json.loads(line) for line in (tmp_path / "execution.jsonl").read_text().splitlines()]
            completed = [row for row in records if row["event"] == "tool_job.worker.end"]
            if any(row["attempt"] == 2 for row in completed):
                break
            time.sleep(0.01)
        else:
            pytest.fail("controlled failing task did not finish")
    finally:
        worker.stop()
        diagnostics.shutdown()
    attempts = [row for row in records if row["event"] == "tool_job.attempt.failed"]
    assert [row["attempt"] for row in attempts] == [1, 2]
    assert {row["report_id"] for row in attempts} == {909}
    assert {row["job_id"] for row in attempts} == {job_id}
    assert trace.run_id not in {row["run_id"] for row in attempts}
    assert len({row["run_id"] for row in attempts}) == 2
    assert len([row for row in records if row["event"] == "tool_job.retry_scheduled"]) == 1
    final = next(row for row in records if row["event"] == "tool_job.dead_lettered")
    assert final["reason_code"] == "attempts_exhausted"
    assert not any(row["event"] == "tool_job.succeeded" for row in records)
