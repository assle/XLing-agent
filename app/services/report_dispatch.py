from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.services.mcp_client import McpToolError, XlingMcpToolClient
from app.services.tool_queue import ToolQueueService


class ReportDispatchError(RuntimeError):
    pass


class ReportDispatcher(Protocol):
    async def dispatch(self, report_id: int, risk_level: str | None) -> None: ...


class QueueReportDispatcher:
    def __init__(self, db: Session, settings: Settings):
        self.queue = ToolQueueService(db, settings)

    async def dispatch(self, report_id: int, risk_level: str | None) -> None:
        self.queue.enqueue_report(report_id, risk_level)


class McpReportDispatcher:
    def __init__(self, settings: Settings):
        self.client = XlingMcpToolClient(settings)

    async def dispatch(self, report_id: int, risk_level: str | None) -> None:
        try:
            await self.client.handle_report(report_id, risk_level)
        except McpToolError as exc:
            raise ReportDispatchError(str(exc)) from exc


def create_report_dispatcher(db: Session, settings: Settings) -> ReportDispatcher:
    if settings.tool_queue_enabled:
        return QueueReportDispatcher(db, settings)
    return McpReportDispatcher(settings)
