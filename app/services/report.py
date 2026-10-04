from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.time import utc_isoformat
from app.models.entities import (
    AlertRecord,
    ChatMessage,
    ChatSession,
    DeadLetterRecord,
    ExcelRecord,
    ReviewRequest,
    SafetyAssessmentRecord,
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
        """保存报告、任务记录和会话查询使用的数据库会话。

        初始化不进行写入或发起模型调用。
        """
        self.db = db

    def latest_reports(self, user_id: int | None = None) -> list[ReportResponse]:
        """按时间倒序返回最多一百条安全评估记录。

        user_id 不为空时只查该用户，否则查询全部；管理员权限由接口层控制。
        """
        query = self.db.query(SafetyAssessmentRecord).order_by(SafetyAssessmentRecord.created_at.desc())
        if user_id is not None:
            query = query.filter(SafetyAssessmentRecord.user_id == user_id)
        return [self._report_response(item) for item in query.limit(100).all()]

    def excel_records(self) -> list[ToolRecordResponse]:
        """返回最近一百条表格写入记录及文件路径。

        只展示数据库记录，不检查对应文件是否仍然存在。
        """
        rows = self.db.query(ExcelRecord).order_by(ExcelRecord.created_at.desc()).limit(100).all()
        return [
            ToolRecordResponse(id=row.id, reportId=row.report_id, status=row.status, message=row.message, createdAt=row.created_at, filePath=row.file_path)
            for row in rows
        ]

    def alert_records(self) -> list[ToolRecordResponse]:
        """返回最近一百条通知记录及渠道和收件人。

        保留真实记录状态，不把所有记录都当作已发送成功。
        """
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
        """返回最近一百条工具任务的执行、重试和依赖信息。

        将内部对象转换为统一响应结构，不改变任务调度状态。
        """
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
        """返回最近一百条失败留存记录，供排查和后续处理。

        包括关联任务、失败原因和保存的参数摘要，不在此重新执行任务。
        """
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
        """按公开编号读取会话及按时间排列的全部消息。

        不在本方法校验用户归属，供经过权限验证的管理接口使用；不存在时抛出 ValueError。
        """
        session = self.db.query(ChatSession).filter(ChatSession.public_id == public_id).first()
        if session is None:
            raise ValueError("Session not found")
        rows = self.db.query(ChatMessage).filter(ChatMessage.session_id == session.id).order_by(ChatMessage.created_at.asc()).all()
        return ConversationResponse(
            sessionId=session.public_id,
            title=session.title,
            noMemory=session.no_memory,
            pendingReview=self._pending_review(session.id),
            messages=[ConversationMessageResponse(role=row.role, content=row.content, createdAt=row.created_at) for row in rows],
        )
    def list_sessions(self, user_id: int) -> list[dict]:
        """列出指定用户最近更新的最多五十个会话。

        为每个会话单独统计消息数，并返回标题、无记忆标志和时间信息。
        """
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
                "createdAt": utc_isoformat(s.created_at),
                "updatedAt": utc_isoformat(s.updated_at),
                "messageCount": msg_count,
            })
        return result

    def conversation_for_user(self, user_id: int, public_id: str) -> ConversationResponse | None:
        """仅返回属于指定用户的会话详情和消息。

        同时按用户编号和公开编号查询，不匹配时返回 None，避免跨用户读取。
        """
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
            pendingReview=self._pending_review(session.id),
            messages=[ConversationMessageResponse(role=row.role, content=row.content, createdAt=row.created_at) for row in rows],
        )

    def _pending_review(self, session_id: int) -> bool:
        return self.db.query(ReviewRequest.id).filter(
            ReviewRequest.session_id == session_id, ReviewRequest.status == "pending",
        ).first() is not None

    def _report_response(self, report: SafetyAssessmentRecord) -> ReportResponse:
        """补充评估记录关联的用户名称和会话公开编号。

        关联对象不存在时使用空文本，其余评估字段按已保存值输出。
        """
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
