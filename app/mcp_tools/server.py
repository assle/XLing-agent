from app.core.bootstrap import create_schema
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.entities import PsychologicalReport
from app.services.tools import ToolOrchestrationService

try:
    from mcp.server.fastmcp import FastMCP
except Exception as exc:  # pragma: no cover
    raise RuntimeError("请先安装 requirements.txt 中的 mcp 依赖") from exc


mcp = FastMCP("xling-tools")


@mcp.tool()
def xling_excel_report(report_id: int) -> str:
    """Write one psychological risk report into the Xling Excel ledger."""
    create_schema()
    db = SessionLocal()
    try:
        report = db.get(PsychologicalReport, report_id)
        if report is None:
            return f"report {report_id} not found"
        record = ToolOrchestrationService(db, get_settings()).write_excel(report)
        return f"success: {record.file_path}"
    finally:
        db.close()


@mcp.tool()
def xling_alert_notify(report_id: int) -> str:
    """Send a high-risk alert email and record the notification result for one psychological report."""
    create_schema()
    db = SessionLocal()
    try:
        report = db.get(PsychologicalReport, report_id)
        if report is None:
            return f"report {report_id} not found"
        record = ToolOrchestrationService(db, get_settings()).notify(report)
        return f"{record.status}: {record.channel} -> {record.recipient}: {record.message}"
    finally:
        db.close()


if __name__ == "__main__":
    mcp.run()
