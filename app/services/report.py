from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.entities import (
    AlertRecord,
    ChatMessage,
    ChatSession,
    DeadLetterRecord,
    ExcelRecord,
    PsychologicalReport,
    ToolJob,
    UserAccount,
)
from app.schemas.dtos import (
    ConversationMessageResponse,
    ConversationResponse,
    DeadLetterResponse,
    ReportResponse,
    ToolJobResponse,
    ToolRecordResponse,
)


class ReportService:
    def __init__(self, db: Session):
        self.db = db

    def latest_reports(self, user_id: int | None = None) -> list[ReportResponse]:
        query = self.db.query(PsychologicalReport).order_by(PsychologicalReport.created_at.desc())
        if user_id is not None:
            query = query.filter(PsychologicalReport.user_id == user_id)
        return [self._report_response(item) for item in query.limit(100).all()]

    def excel_records(self) -> list[ToolRecordResponse]:
        rows = self.db.query(ExcelRecord).order_by(ExcelRecord.created_at.desc()).limit(100).all()
        return [
            ToolRecordResponse(id=row.id, reportId=row.report_id, status=row.status, message=row.message, createdAt=row.created_at, filePath=row.file_path)
            for row in rows
        ]

    def alert_records(self) -> list[ToolRecordResponse]:
        rows = self.db.query(AlertRecord).order_by(AlertRecord.created_at.desc()).limit(100).all()
        return [
            ToolRecordResponse(
                id=row.id,
                reportId=row.report_id,
                status=row.status,
                message=row.message,
                createdAt=row.created_at,
                channel=row.channel,
                recipient=row.recipient,
            )
            for row in rows
        ]

    def tool_jobs(self) -> list[ToolJobResponse]:
        rows = self.db.query(ToolJob).order_by(ToolJob.created_at.desc()).limit(100).all()
        return [
            ToolJobResponse(
                id=row.id,
                reportId=row.report_id,
                kind=row.kind,
                status=row.status,
                attempts=row.attempts,
                maxAttempts=row.max_attempts,
                dependsOnJobId=row.depends_on_job_id,
                runAfter=row.run_after,
                lastError=row.last_error,
                createdAt=row.created_at,
                updatedAt=row.updated_at,
            )
            for row in rows
        ]

    def dead_letters(self) -> list[DeadLetterResponse]:
        rows = self.db.query(DeadLetterRecord).order_by(DeadLetterRecord.created_at.desc()).limit(100).all()
        return [
            DeadLetterResponse(
                id=row.id,
                jobId=row.job_id,
                reportId=row.report_id,
                kind=row.kind,
                reason=row.reason,
                payload=row.payload,
                createdAt=row.created_at,
            )
            for row in rows
        ]

    def conversation(self, public_id: str) -> ConversationResponse:
        session = self.db.query(ChatSession).filter(ChatSession.public_id == public_id).first()
        if session is None:
            raise ValueError("Session not found")
        rows = self.db.query(ChatMessage).filter(ChatMessage.session_id == session.id).order_by(ChatMessage.created_at.asc()).all()
        return ConversationResponse(
            sessionId=session.public_id,
            title=session.title,
            noMemory=session.no_memory,
            messages=[ConversationMessageResponse(role=row.role, content=row.content, createdAt=row.created_at) for row in rows],
        )
    def list_sessions(self, user_id: int) -> list[dict]:
        sessions = (
            self.db.query(ChatSession)
            .filter(ChatSession.user_id == user_id)
            .order_by(ChatSession.updated_at.desc())
            .limit(50)
            .all()
        )
        result = []
        for s in sessions:
            msg_count = self.db.query(ChatMessage).filter(ChatMessage.session_id == s.id).count()
            result.append({
                "sessionId": s.public_id,
                "title": s.title,
                "noMemory": s.no_memory,
                "createdAt": s.created_at.isoformat(),
                "updatedAt": s.updated_at.isoformat(),
                "messageCount": msg_count,
            })
        return result

    def conversation_for_user(self, user_id: int, public_id: str) -> ConversationResponse | None:
        session = self.db.query(ChatSession).filter(
            ChatSession.public_id == public_id, ChatSession.user_id == user_id
        ).first()
        if session is None:
            return None
        rows = self.db.query(ChatMessage).filter(ChatMessage.session_id == session.id).order_by(ChatMessage.created_at.asc()).all()
        return ConversationResponse(
            sessionId=session.public_id,
            title=session.title,
            noMemory=session.no_memory,
            messages=[ConversationMessageResponse(role=row.role, content=row.content, createdAt=row.created_at) for row in rows],
        )

    def _report_response(self, report: PsychologicalReport) -> ReportResponse:
        user = self.db.get(UserAccount, report.user_id)
        session = self.db.get(ChatSession, report.session_id)
        return ReportResponse(
            id=report.id,
            sessionId=session.public_id if session else "",
            username=user.username if user else "",
            displayName=user.display_name if user else "",
            content=report.content,
            intent=report.intent,
            emotion=report.emotion,
            emotionScore=report.emotion_score,
            riskLevel=report.risk_level,
            confidence=report.confidence,
            summary=report.summary,
            createdAt=report.created_at,
        )
