import json
import sys

from openpyxl import load_workbook

from app.core.config import Settings
from app.core.database import SessionLocal
from app.models.entities import AlertRecord, ExcelRecord, SafetyAssessmentRecord, ToolJob
from app.services.tool_queue import ToolQueueService
from app.services.tools import ToolOrchestrationService

report_id = int(sys.argv[1])
settings = Settings()
with SessionLocal() as db:
    report = db.get(SafetyAssessmentRecord, report_id)
    assert report is not None and report.risk_level == 'HIGH'
    queue = ToolQueueService(db, settings)
    original = [job.id for job in db.query(ToolJob).filter_by(report_id=report_id).order_by(ToolJob.id)]
    assert len(original) == 2
    assert all(job.status == 'SUCCESS' for job in db.query(ToolJob).filter_by(report_id=report_id))
    for _ in range(2):
        assert sorted(job.id for job in queue.enqueue_report(report_id, 'HIGH')) == original
    tools = ToolOrchestrationService(db, settings)
    tools.write_excel(report)
    tools.notify(report)
    workbook = load_workbook(settings.excel_path)
    try:
        rows = sum(row[0].value == report_id for row in list(workbook.active.rows)[1:])
    finally:
        workbook.close()
    result = {'reportId':report_id, 'jobIDs':original,
              'jobs':db.query(ToolJob).filter_by(report_id=report_id).count(),
              'excelSuccessRecords':db.query(ExcelRecord).filter_by(report_id=report_id,status='SUCCESS').count(),
              'alertFailedRecords':db.query(AlertRecord).filter_by(report_id=report_id,status='FAILED').count(),
              'alertSuccessRecords':db.query(AlertRecord).filter_by(report_id=report_id,status='SUCCESS').count(),
              'workbookRowsForReport':rows}
    assert result['jobs'] == 2 and result['excelSuccessRecords'] == 1 and result['alertSuccessRecords'] == 1 and rows == 1
    print('CLOSING_RESULT='+json.dumps(result))
