from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from app.core import diagnostics
from app.core.config import Settings
from app.core.enums import RiskLevel


class McpToolError(RuntimeError):
    pass


class XlingMcpToolClient:
    def __init__(self, settings: Settings):
        """保存工具子进程的运行配置。

        初始化不启动进程或发送外部通知。
        """
        self.settings = settings

    async def handle_report(self, report_id: int, risk_level: str | None) -> list[str]:
        """在一次工具会话中先写表格，高风险时再请求通知。

        返回每次调用的结果文字；首个调用失败会停止后续步骤，其他异常包装成统一工具错误。
        """
        with diagnostics.stage("mcp.report", report_id=report_id):
            try:
                async with self._session() as session:
                    results = [
                        await self._call_tool(session, "xling_excel_report", {"report_id": report_id}),
                    ]
                    if risk_level == RiskLevel.HIGH.value:
                        results.append(await self._call_tool(session, "xling_alert_notify", {"report_id": report_id}))
                    return results
            except McpToolError:
                raise
            except Exception as exc:
                raise McpToolError(f"MCP 工具调用异常：{type(exc).__name__}: {exc}") from exc

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[Any]:
        """启动工具子进程并提供初始化后的通信会话。

        MCP（模型与工具交换请求的协议）通过标准输入输出通信；使用当前解释器和项目目录。
        上下文管理器在调用结束时释放通信资源，不把会话留给后续请求共享。
        """
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise McpToolError("缺少 mcp 依赖，无法通过 MCP 调用 Xling 工具") from exc

        project_root = self.settings.project_root
        env = os.environ.copy()
        python_path = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(project_root) if not python_path else f"{project_root}{os.pathsep}{python_path}"

        server = StdioServerParameters(
            command=sys.executable,
            args=["-m", "app.mcp_tools.server"],
            env=env,
            cwd=str(project_root),
        )
        async with stdio_client(server) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session

    async def _call_tool(self, session: Any, name: str, arguments: dict[str, Any]) -> str:
        """发送工具名及参数，并检查工具是否显式报告错误。

        显式错误转为 McpToolError；成功时返回整理后的文字，文字本身仍可能包含业务状态。
        """
        with diagnostics.stage("mcp.call", tool=name, report_id=arguments.get("report_id")):
            result = await session.call_tool(name, arguments=arguments)
            message = self._result_message(result)
            failed = bool(getattr(result, "isError", False))
            diagnostics.emit("mcp.result", tool=name, status="failed" if failed else "success")
            if failed:
                raise McpToolError(f"{name} 调用失败：{message}")
            return message

    def _result_message(self, result: Any) -> str:
        """把工具结果中的内容块整理为可阅读文本。

        优先连接文本内容，没有内容块时使用结构化结果或整个结果对象的文本表示。
        """
        parts = []
        for item in getattr(result, "content", []) or []:
            text = getattr(item, "text", None)
            parts.append(text if text is not None else str(item))
        if parts:
            return "\n".join(parts)
        structured = getattr(result, "structuredContent", None)
        return str(structured if structured is not None else result)
