from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from app.core import diagnostics
from app.core.config import Settings
from app.services.mcp_client import McpToolError, XlingMcpToolClient
from app.services.tool_queue import ToolQueueService


class ReportDispatchError(RuntimeError):
    pass


class ReportDispatcher(Protocol):
    async def dispatch(self, report_id: int, risk_level: str | None) -> None:
        """约定安全评估记录后处理的异步入口。

        report_id 指定记录，risk_level 决定是否需要高风险通知；具体执行方式由实现类负责。
        """
        ...


class QueueReportDispatcher:
    def __init__(self, db: Session, settings: Settings):
        """创建用于登记后处理任务的队列服务。

        复用传入数据库及配置，不在初始化时执行表格或通知任务。
        """
        self.queue = ToolQueueService(db, settings)

    async def dispatch(self, report_id: int, risk_level: str | None) -> None:
        """把表格及必要通知任务登记到队列。

        本函数虽提供异步接口，内部登记操作仍是同步数据库调用；返回不代表任务已执行完成。
        """
        with diagnostics.stage("report.dispatch", report_id=report_id, risk_level=risk_level, backend="queue"):
            self.queue.enqueue_report(report_id, risk_level)


class McpReportDispatcher:
    def __init__(self, settings: Settings):
        """准备通过独立工具进程处理报告的客户端。

        仅保存配置，实际进程由调用时创建。
        """
        self.client = XlingMcpToolClient(settings)

    async def dispatch(self, report_id: int, risk_level: str | None) -> None:
        """通过工具客户端处理安全评估记录。

        将约定的工具错误转换成报告调度错误，供聊天入口发送明确失败事件。
        """
        with diagnostics.stage("report.dispatch", report_id=report_id, risk_level=risk_level, backend="mcp"):
            try:
                await self.client.handle_report(report_id, risk_level)
            except McpToolError as exc:
                raise ReportDispatchError(str(exc)) from exc


def create_report_dispatcher(db: Session, settings: Settings) -> ReportDispatcher:
    """根据工具队列开关选择报告处理方式。

    启用时只入队，关闭时通过工具客户端调用表格及通知操作。
    """
    if settings.tool_queue_enabled:
        return QueueReportDispatcher(db, settings)
    return McpReportDispatcher(settings)
