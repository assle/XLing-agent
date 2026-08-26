"""User data deletion service (issue 13).

Deletes all user-related data in a single transaction with rollback on failure.
After deletion, the user account and JWT token are immediately invalid.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.entities import (
    UserAccount, UserProfile, MemoryCard, ScreeningResult,
    ActionPlan, ActionPlanItem, CheckIn, RiskTrajectoryPoint,
    ChatMessage, ChatSession, PsychologicalReport,
    ReviewRequest, AlertRecord, ExcelRecord, ToolJob, DeadLetterRecord,
)

logger = logging.getLogger(__name__)


PRIVACY_NOTICE = """# Xling 隐私说明

## 我们收集什么

- **账号信息**：用户名、显示名、密码哈希（bcrypt）、角色
- **备考画像**：备考阶段、目标考试、考试日期（可选，可跳过）
- **记忆卡片**：你创建或确认的长期记忆条目（可查看、编辑、删除）
- **量表筛查**：PHQ-9 / GAD-7 的逐题答案、分数、时间和触发来源（仅自愿）
- **行动计划与次日反馈**：认知行为四维追问后生成的行动条目、完成状态和次日反馈结果
- **会话消息**：你与系统的对话消息
- **安全风险记录**：风险等级、情绪标签、风险评估摘要（学生端不展示）
- **人工审核记录**：审核触发原因、脱敏摘要、审核决定和备注

## 用途与可见范围

- 备考画像和记忆卡片用于提供阶段感知的个性化支持
- 量表筛查仅用于筛查和趋势参考，不作诊断
- 安全风险记录用于安全风险评估和人工审核，仅管理员可见
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
    """Delete all user data in a transaction (issue 13)."""

    def __init__(self, db: Session):
        self.db = db

    def delete_all_user_data(self, user_id: int) -> dict:
        """Delete all data for a user in a single transaction.

        Returns a summary of deleted item counts.
        Returns empty dict on failure (transaction rolled back).
        """
        counts = {}
        try:
            # Get user's session IDs and report IDs
            session_ids = [
                s.id for s in self.db.query(ChatSession)
                .filter(ChatSession.user_id == user_id).all()
            ]
            report_ids = [
                r.id for r in self.db.query(PsychologicalReport)
                .filter(PsychologicalReport.user_id == user_id).all()
            ]

            # Delete in dependency order (children first)
            counts["memory_cards"] = self._delete(MemoryCard, MemoryCard.user_id == user_id)
            counts["screening_results"] = self._delete(ScreeningResult, ScreeningResult.user_id == user_id)
            counts["risk_trajectory"] = self._delete(RiskTrajectoryPoint, RiskTrajectoryPoint.user_id == user_id)
            counts["user_profiles"] = self._delete(UserProfile, UserProfile.user_id == user_id)

            # CheckIns (via ActionPlan)
            if session_ids:
                plan_ids = [
                    p.id for p in self.db.query(ActionPlan)
                    .filter(ActionPlan.user_id == user_id).all()
                ]
                if plan_ids:
                    counts["check_ins"] = self._delete(CheckIn, CheckIn.plan_id.in_(plan_ids))
                    counts["action_plan_items"] = self._delete(ActionPlanItem, ActionPlanItem.plan_id.in_(plan_ids))
                counts["action_plans"] = self._delete(ActionPlan, ActionPlan.user_id == user_id)

            # Messages
            counts["chat_messages"] = self._delete(ChatMessage, ChatMessage.user_id == user_id)

            # Reviews (via sessions)
            if session_ids:
                counts["review_requests"] = self._delete(ReviewRequest, ReviewRequest.session_id.in_(session_ids))

            # Tool-related records (via reports)
            if report_ids:
                counts["alert_records"] = self._delete(AlertRecord, AlertRecord.report_id.in_(report_ids))
                counts["excel_records"] = self._delete(ExcelRecord, ExcelRecord.report_id.in_(report_ids))
                counts["tool_jobs"] = self._delete(ToolJob, ToolJob.report_id.in_(report_ids))
                counts["dead_letters"] = self._delete(DeadLetterRecord, DeadLetterRecord.report_id.in_(report_ids))

            # Reports
            counts["psychological_reports"] = self._delete(PsychologicalReport, PsychologicalReport.user_id == user_id)

            # Sessions
            counts["chat_sessions"] = self._delete(ChatSession, ChatSession.user_id == user_id)

            # User account (last)
            counts["user_account"] = self._delete(UserAccount, UserAccount.id == user_id)

            self.db.commit()
            logger.info("User %s data deleted: %s", user_id, counts)
            return counts
        except Exception as exc:
            self.db.rollback()
            logger.error("User %s data deletion failed, rolled back: %s", user_id, exc)
            raise

    def _delete(self, model, filter_clause) -> int:
        result = self.db.query(model).filter(filter_clause).delete(synchronize_session="fetch")
        return result
