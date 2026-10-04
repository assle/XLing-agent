from app.core.bootstrap import create_schema
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.entities import SafetyAssessmentRecord
from app.services.tools import ToolOrchestrationService

try:
    from mcp.server.fastmcp import FastMCP  # type: ignore[attr-defined]
except Exception as exc:  # pragma: no cover
    raise RuntimeError("请先安装 requirements.txt 中的 mcp 依赖") from exc


mcp = FastMCP("xling-tools")


@mcp.tool()
def xling_excel_report(report_id: int) -> str:
    # 按安全评估记录编号调用表格写入服务。
    # report_id 指定已有记录，缺失时返回提示文本；始终关闭本次数据库会话。
    # 此工具的原始说明供外部工具协议读取，中文教学说明使用普通注释保留其对外定义。
    """Write one psychological risk report into the Xling Excel ledger."""
    create_schema()
    db = SessionLocal()
    try:
        report = db.get(SafetyAssessmentRecord, report_id)
        if report is None:
            return f"report {report_id} not found"
        record = ToolOrchestrationService(db, get_settings()).write_excel(report)
        return f"success: {record.file_path}"
    finally:
        db.close()


@mcp.tool()
def xling_alert_notify(report_id: int) -> str:
    # 按安全评估记录编号执行通知服务并返回投递状态。
    # 是否真的发送邮件取决于通知配置；返回文字中的状态应单独检查，不能只凭工具调用成功判断发送完成。
    # 无论成功或失败都关闭本次数据库会话。
    """Send a high-risk alert email and record the notification result for one psychological report."""
    create_schema()
    db = SessionLocal()
    try:
        report = db.get(SafetyAssessmentRecord, report_id)
        if report is None:
            return f"report {report_id} not found"
        record = ToolOrchestrationService(db, get_settings()).notify(report)
        return f"{record.status}: {record.channel} -> {record.recipient}: {record.message}"
    finally:
        db.close()


if __name__ == "__main__":
    mcp.run()
