from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.agents.runtime import AgentRunResult
from app.core import diagnostics
from app.core.config import Settings
from app.core.enums import MessageRole, RiskLevel, ToolJobKind, ToolJobStatus
from app.core.time import utc_now
from app.models.entities import (
    ChatMessage,
    ChatSession,
    ReviewRequest,
    SafetyAssessmentRecord,
    ToolJob,
    UserAccount,
)
from app.services.review import HANDOFF_REASONS


@dataclass(frozen=True)
class PersistedSupportTurn:
    message: ChatMessage
    report: SafetyAssessmentRecord | None
    review: ReviewRequest | None
    jobs: list[ToolJob]


class SupportTurnTransaction:
    """协调一轮支持过程中的数据库写入，在记录准备完成后统一确认保存。"""

    def __init__(
        self,
        db: Session,
        settings: Settings,
        *,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        """准备一轮支持过程所需的数据库会话、配置及故障模拟入口。

        db 和 settings 由调用方提供；初始化仅保存依赖，不立即写入数据。
        fault_injector 用于测试在指定阶段主动抛错；未提供时使用不执行任何操作的匿名函数。
        """
        self.db = db
        self.settings = settings
        # 未注入故障模拟函数时，各阶段检查都不执行额外操作。
        self.fault_injector = fault_injector or (lambda stage: None)

    def save_support_turn(
        self,
        *,
        user: UserAccount,
        session: ChatSession,
        content: str,
        run: AgentRunResult,
        handoff_reason: str = "HIGH_RISK_KEYWORD",
        desensitized_summary: str = "",
    ) -> PersistedSupportTurn:
        """将用户消息及本轮需要的评估记录、人工审核请求和工具任务统一保存。

        user、session 确定归属，content 是原始消息，run 是本轮处理结果；后两个参数描述人工审核原因和脱敏摘要。
        返回包含消息、可选评估记录、可选审核请求及任务列表的对象；未启用的分支保持为空。
        所有记录准备完毕后才确认保存；提交前失败会撤销尚未提交的变更。提交后的刷新若失败，已经提交的数据不会被随后调用的 rollback 撤销。
        """
        report = None
        review = None
        jobs: list[ToolJob] = []
        try:
            message = ChatMessage(
                user_id=user.id,
                session_id=session.id,
                role=MessageRole.USER.value,
                content=content,
            )
            session.touch()
            self.db.add_all([message, session])
            # 先把待写入内容发送到数据库以取得编号，但此时尚未确认保存。
            # 后续评估、审核或任务准备失败时，这些未提交的记录仍可一起撤销。
            self.db.flush()
            self.fault_injector("message")

            # 只有本轮明确需要记录且确实有评估结果时，才建立相关记录和后续任务。
            if run.requires_report and run.assessment is not None:
                assessment = run.assessment
                report = SafetyAssessmentRecord(
                    user_id=user.id,
                    session_id=session.id,
                    content=content,
                    intent=run.intent.value,
                    emotion=assessment.emotion.value,
                    emotion_score=assessment.emotion_score,
                    risk_level=run.risk_level.value,
                    confidence=assessment.confidence,
                    summary=assessment.summary,
                )
                self.db.add(report)
                self.db.flush()
                self.fault_injector("report")

                # 审核请求引用刚写入的评估编号；先确认原因属于允许集合，再准备审核记录。
                if run.pending_review:
                    if handoff_reason not in HANDOFF_REASONS:
                        raise ValueError(f"Invalid handoff reason: {handoff_reason}")
                    review = ReviewRequest(
                        session_id=session.id,
                        report_id=report.id,
                        thread_id=session.public_id,
                        risk_summary=assessment.summary,
                        handoff_reason=handoff_reason,
                        desensitized_summary=desensitized_summary,
                        status="pending",
                    )
                    self.db.add(review)
                    self.db.flush()
                self.fault_injector("review")

                # 此处仅登记待执行任务，不直接写表格或发送通知。
                if self.settings.tool_queue_enabled:
                    excel_job = self._job(report.id, ToolJobKind.EXCEL_REPORT.value)
                    self.db.add(excel_job)
                    self.db.flush()
                    jobs.append(excel_job)
                    if run.risk_level == RiskLevel.HIGH:
                        # 让风险通知依赖表格任务，任务执行器会据此安排先后顺序。
                        alert_job = self._job(
                            report.id,
                            ToolJobKind.RISK_ALERT.value,
                            depends_on_job_id=excel_job.id,
                        )
                        self.db.add(alert_job)
                        self.db.flush()
                        jobs.append(alert_job)
                # 测试可在任务准备完毕后模拟故障，验证整轮记录能否在提交前一起撤销。
                self.fault_injector("jobs")

            # 在这一处确认保存消息及其关联记录，避免分步提交留下不完整的一轮数据。
            links = {
                "user_message_id": message.id,
                "report_id": report.id if report is not None else None,
                "review_id": review.id if review is not None else None,
            }
            job_links = [
                {"job_id": job.id, "report_id": job.report_id, "job_kind": job.kind,
                 "dependency_job_id": job.depends_on_job_id, "reused": False}
                for job in jobs
            ]
            self.db.commit()
            diagnostics.bind(**links)
            diagnostics.emit("support_turn.persisted", **links)
            for metadata in job_links:
                diagnostics.emit("tool_job.registered", **metadata)
            # 提交已经完成；刷新只是读取数据库保存后的字段，不是另一轮写入。
            for row in [message, report, review, *jobs]:
                if row is not None:
                    self.db.refresh(row)
            return PersistedSupportTurn(message, report, review, jobs)
        except Exception:
            # 撤销当前尚未提交的变更并继续抛出原异常；不能撤回之前已经成功的提交。
            self.db.rollback()
            raise

    def _job(
        self,
        report_id: int,
        kind: str,
        *,
        depends_on_job_id: int | None = None,
    ) -> ToolJob:
        """构造一条尚未加入数据库的待执行工具任务。

        report_id 关联安全评估记录，kind 指定任务种类，depends_on_job_id 可要求先完成另一任务。
        返回初始重试次数为零、按配置限制重试次数的任务对象；实际保存由调用方负责。
        """
        return ToolJob(
            report_id=report_id,
            kind=kind,
            status=ToolJobStatus.PENDING.value,
            attempts=0,
            max_attempts=self.settings.tool_queue_max_attempts,
            depends_on_job_id=depends_on_job_id,
            run_after=utc_now(),
            last_error="",
        )
