"""User data deletion service (issue 13).

Deletes business data in one database transaction. Persistent graph checkpoints
are cleaned only after that commit, so a business rollback cannot orphan the
still-existing account from its interrupted review state.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.time import utc_now
from app.models.entities import (
    ActionPlan,
    ActionPlanItem,
    AlertRecord,
    ChatMessage,
    ChatSession,
    CheckIn,
    CheckpointDeletionTask,
    DeadLetterRecord,
    ExcelRecord,
    MemoryCard,
    ReviewRequest,
    RiskTrajectoryPoint,
    SafetyAssessmentRecord,
    ScreeningResult,
    ToolJob,
    UserAccount,
    UserProfile,
)
from app.services.memory import RedisShortTermMemoryStore
from app.services.tools import EXCEL_WRITE_LOCK, ToolOrchestrationService

logger = logging.getLogger(__name__)


PRIVACY_NOTICE = """# Xling 隐私说明

## 我们收集什么

- **账号信息**：用户名、显示名、密码哈希（bcrypt）、角色
- **支持背景**：当前关注问题、支持目标、偏好支持方式（可选，可跳过）
- **记忆卡片**：你创建或确认的长期记忆条目（可查看、编辑、删除）
- **量表筛查**：PHQ-9 / GAD-7 的逐题答案、分数、时间和触发来源（仅自愿）
- **行动计划与次日反馈**：认知行为四维追问后生成的行动条目、完成状态和次日反馈结果
- **会话消息**：你与系统的对话消息
- **安全评估记录**：风险等级、情绪标签、风险评估摘要（用户端不展示）
- **人工审核记录**：审核触发原因、脱敏摘要、审核决定和备注

## 用途与可见范围

- 支持背景和记忆卡片用于提供用户选择的个性化支持
- 量表筛查仅用于筛查和趋势参考，不作诊断
- 安全评估记录用于安全风险评估和人工审核，仅授权审核人员可见
- 无记忆会话不产生新的长期记忆，但仍保存安全必需的风险和人工审核记录

## 保存与删除

- 你可以随时通过数据删除入口删除你的所有数据
- 删除是不可逆的，会移除所有关联数据
- 无记忆会话的安全记录在删除时一并清除
- 如有法律要求保留的最小记录，将在匿名化后保留

## 无记忆会话

- 无记忆会话不读取或生成记忆卡片，不自动更新画像
- 无记忆会话仍保存当次会话连续性和安全必需记录
- 无记忆会话不等于删除已有记忆
"""


class DataDeletionService:
    """协调业务数据删除与提交后的执行状态清理，保留失败清理任务供后续重试。"""

    def __init__(self, db: Session, settings: Settings | None = None):
        """保存用户数据删除所需的数据库会话和配置。

        未显式传入配置时读取应用配置，初始化不删除任何记录。
        """
        self.db = db
        self.settings = settings or get_settings()

    def delete_all_user_data(self, user_id: int) -> dict:
        """清理指定用户的会话缓存和台账，再删除业务数据及执行状态。

        缓存或台账清理失败时保留账户并回滚业务删除，用户可以重试；其他用户的键和台账行不受影响。
        外部清理不共享业务事务，后续业务提交失败可能已经清除缓存或台账，但账户及原始业务数据保留。
        业务删除失败会回滚并抛出异常；后续状态清理失败则返回待清理标志，不撤销已经完成的业务删除。
        """
        counts = {}
        # 和台账写入串行到业务提交，防止已领取的工具任务在删除后重新导出记录。
        EXCEL_WRITE_LOCK.acquire()
        try:
            # 先收集关联编号，为随后逐表删除和执行状态清理提供依据。
            session_ids = [
                s.id for s in self.db.query(ChatSession)
                .filter(ChatSession.user_id == user_id).all()
            ]
            thread_ids = [
                s.public_id for s in self.db.query(ChatSession)
                .filter(ChatSession.user_id == user_id).all()
            ]
            report_ids = [
                r.id for r in self.db.query(SafetyAssessmentRecord)
                .filter(SafetyAssessmentRecord.user_id == user_id).all()
            ]
            ledger_paths = [self.settings.excel_path, *[
                row.file_path for row in self.db.query(ExcelRecord)
                .filter(ExcelRecord.report_id.in_(report_ids)).all()
            ]]
            counts["cached_session_keys"] = RedisShortTermMemoryStore(self.settings).delete_sessions(thread_ids)
            counts["ledger_rows"] = ToolOrchestrationService.delete_excel_reports(report_ids, ledger_paths)
            # 把清理任务和业务删除放进同一次提交，确保账户删除后仍有可重试的清理依据。
            checkpoint_task = self._create_checkpoint_deletion_task(thread_ids)

            # 按依赖顺序先删除关联记录，再删除会话和账户。
            counts["memory_cards"] = self._delete(MemoryCard, MemoryCard.user_id == user_id)
            counts["screening_results"] = self._delete(ScreeningResult, ScreeningResult.user_id == user_id)
            counts["risk_trajectory"] = self._delete(RiskTrajectoryPoint, RiskTrajectoryPoint.user_id == user_id)
            counts["user_profiles"] = self._delete(UserProfile, UserProfile.user_id == user_id)

            # 通过行动计划定位反馈和行动项。
            # 当前实现以存在会话为条件清理行动计划分支；没有会话的特殊数据并不经过此分支。
            if session_ids:
                plan_ids = [
                    p.id for p in self.db.query(ActionPlan)
                    .filter(ActionPlan.user_id == user_id).all()
                ]
                if plan_ids:
                    counts["check_ins"] = self._delete(CheckIn, CheckIn.plan_id.in_(plan_ids))
                    counts["action_plan_items"] = self._delete(ActionPlanItem, ActionPlanItem.plan_id.in_(plan_ids))
                counts["action_plans"] = self._delete(ActionPlan, ActionPlan.user_id == user_id)

            # 删除用户的会话消息。
            counts["chat_messages"] = self._delete(ChatMessage, ChatMessage.user_id == user_id)

            # 通过会话编号删除相关审核请求。
            if session_ids:
                counts["review_requests"] = self._delete(ReviewRequest, ReviewRequest.session_id.in_(session_ids))

            # 通过评估编号清理通知、表格、队列和失败留存记录。
            if report_ids:
                counts["alert_records"] = self._delete(AlertRecord, AlertRecord.report_id.in_(report_ids))
                counts["excel_records"] = self._delete(ExcelRecord, ExcelRecord.report_id.in_(report_ids))
                counts["tool_jobs"] = self._delete(ToolJob, ToolJob.report_id.in_(report_ids))
                counts["dead_letters"] = self._delete(DeadLetterRecord, DeadLetterRecord.report_id.in_(report_ids))

            # 删除安全评估记录。
            counts["safety_assessment_records"] = self._delete(SafetyAssessmentRecord, SafetyAssessmentRecord.user_id == user_id)

            # 删除用户会话。
            counts["chat_sessions"] = self._delete(ChatSession, ChatSession.user_id == user_id)

            # 最后删除账户本身，前面先处理引用它的业务记录，减少关系约束冲突。
            counts["user_account"] = self._delete(UserAccount, UserAccount.id == user_id)

            self.db.commit()
            logger.info("User %s data deleted: %s", user_id, counts)
        except Exception as exc:
            self.db.rollback()
            logger.error("User %s data deletion failed, rolled back: %s", user_id, exc)
            raise
        finally:
            EXCEL_WRITE_LOCK.release()
        # 业务删除已确认成功后才动独立状态库，避免业务回滚后失去人工审核恢复依据。
        try:
            pending = bool(
                checkpoint_task
                and not self._process_checkpoint_deletion_task(checkpoint_task.id)
            )
        except Exception:
            pending = bool(checkpoint_task)
        counts["checkpoint_cleanup_pending"] = int(pending)
        return counts

    def _delete(self, model, filter_clause) -> int:
        """按给定条件批量删除某类数据库记录，并返回影响行数。

        使用当前会话执行，提交由外层统一负责；过滤条件由调用方限定用户归属。
        """
        result = self.db.query(model).filter(filter_clause).delete(synchronize_session="fetch")
        return result

    def _create_checkpoint_deletion_task(
        self,
        thread_ids: list[str],
    ) -> CheckpointDeletionTask | None:
        """为待删除用户的文件执行状态登记持久化清理任务。

        仅在有会话编号、采用文件后端且文件存在时创建；先取得任务编号，不单独提交。
        """
        if not thread_ids or self.settings.langgraph_checkpoint_backend.lower() not in {
            "sqlite",
            "async_sqlite",
        }:
            return None
        path = Path(self.settings.langgraph_checkpoint_path)
        if not path.is_absolute():
            path = self.settings.project_root / path
        if not path.exists():
            return None
        task = CheckpointDeletionTask(
            checkpoint_path=str(path),
            thread_ids_json=json.dumps(thread_ids),
            status="pending",
        )
        self.db.add(task)
        self.db.flush()
        return task

    def retry_pending_checkpoint_deletions(self) -> int:
        """逐个重试所有待处理的执行状态清理任务。

        先收集编号再处理，避免删除记录影响遍历；返回本次处理成功的数量。
        """
        task_ids = [
            row.id
            for row in self.db.query(CheckpointDeletionTask)
            .filter(CheckpointDeletionTask.status == "pending")
            .all()
        ]
        return sum(self._process_checkpoint_deletion_task(task_id) for task_id in task_ids)

    def _process_checkpoint_deletion_task(self, task_id: int) -> bool:
        """执行一条状态清理任务，成功后删除任务记录。

        失败时恢复数据库会话并保留待处理任务，累计尝试次数和错误类型；任务已不存在也视为完成。
        """
        task = self.db.get(CheckpointDeletionTask, task_id)
        if task is None:
            return True
        try:
            thread_ids = [str(item) for item in json.loads(task.thread_ids_json)]
            self._delete_checkpoint_rows(thread_ids, Path(task.checkpoint_path))
            # 文件状态清理成功后再删除任务依据，避免失败时失去可重试记录。
            self.db.delete(task)
            self.db.commit()
            return True
        except Exception as exc:
            self.db.rollback()
            task = self.db.get(CheckpointDeletionTask, task_id)
            if task is not None:
                task.status = "pending"
                # 保留失败次数和异常类型便于重试排查，不把文件清理失败当作业务删除失败。
                task.attempts += 1
                task.last_error = type(exc).__name__
                task.updated_at = utc_now()
                self.db.add(task)
                self.db.commit()
            logger.error(
                "Checkpoint deletion task %s needs retry: %s",
                task_id,
                type(exc).__name__,
            )
            return False

    @staticmethod
    def _delete_checkpoint_rows(thread_ids: list[str], path: Path) -> None:
        """从指定状态数据库中删除给定会话编号对应的数据。

        只处理实际存在的三类状态表，使用参数绑定传入编号；统一提交后关闭独立连接，文件不存在时直接返回。
        """
        if not thread_ids or not path.exists():
            return
        placeholders = ",".join("?" for _ in thread_ids)
        connection = sqlite3.connect(path)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            for table in ("writes", "checkpoints", "xling_checkpoint_activity"):
                if table in tables:
                    connection.execute(
                        f"DELETE FROM {table} WHERE thread_id IN ({placeholders})",
                        thread_ids,
                    )
            connection.commit()
        finally:
            connection.close()
