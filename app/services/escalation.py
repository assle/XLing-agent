"""Risk-triggered closed-loop escalation (issue 12).

Connects risk trajectory, screening, next-day feedback, and human review into
a complete safety-escalation path:

  进入 -> 消息分流 -> 安全风险评估 -> 认知行为四维追问 -> 行动计划 -> 次日反馈 -> 安全升级或完成

Escalation triggers:
  - RISK_TRAJECTORY_RISING: trajectory rising past threshold
  - SUSTAINED_NO_IMPROVEMENT: next-day feedback reports worsening or no improvement
  - HIGH_RISK_KEYWORD: high-risk screening answers
  - USER_REQUEST: user explicitly requests human support
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from app.core.enums import RiskLevel
from app.services.privacy import PrivacySanitizer
from app.services.review import ReviewService

logger = logging.getLogger(__name__)


@dataclass
class EscalationResult:
    """Result of an escalation check."""
    should_escalate: bool
    handoff_reason: Optional[str] = None
    desensitized_summary: str = ""
    user_message: str = ""
    review_id: Optional[int] = None


# 安全升级时使用的统一用户提示。
SAFETY_MESSAGE = (
    "我注意到你可能需要更多支持。你的情况已进入人工审核流程。"
    "如果你现在处于紧急情况，请立刻联系身边可信任的人、"
    "当地紧急服务或部署方提供的专业支持与当地紧急资源。"
)

# 自愿筛查邀请文字，不强制用户作答。
SCREENING_SUGGESTION = (
    "我注意到你最近的情绪有一些变化。如果你愿意，可以完成一次简短的自愿量表筛查（PHQ-9 或 GAD-7），"
    "这可以帮助你更好地了解自己的状态。这只是自愿的筛查，不会影响你和我的对话。"
)


class EscalationService:
    """把风险轨迹、自愿筛查、次日反馈和主动求助连接到人工审核流程。"""

    def __init__(self, db: Session, review_svc: ReviewService | None = None):
        """保存数据库和可选人工审核服务，供各类触发入口共用。

        未提供审核服务时仍可计算需要升级的结果，但不会建立审核记录。
        """
        self.db = db
        self.review_svc = review_svc

    def check_trajectory_escalation(
        self,
        user_id: int,
        session_id: int | None,
        report_id: int | None,
        thread_id: str,
        current_risk: RiskLevel,
        trajectory_rising: bool,
        current_difficulty: str = "",
        risk_trend: str = "",
    ) -> EscalationResult:
        """在单次风险尚非高风险但轨迹上升时准备安全升级。

        已明确高风险的情况交给其他路径；无上升标志时返回无需升级的结果。
        """
        if current_risk == RiskLevel.HIGH:
            # 当前已明确高风险的情况由独立安全路径处理。
            return EscalationResult(should_escalate=False)

        if not trajectory_rising:
            return EscalationResult(should_escalate=False)

        summary = PrivacySanitizer.build_review_summary(
            current_difficulty=current_difficulty,
            risk_trend=risk_trend or "风险轨迹连续上升",
        )
        return self._create_escalation(
            user_id, session_id, report_id, thread_id,
            handoff_reason="RISK_TRAJECTORY_RISING",
            desensitized_summary=summary,
        )

    def check_checkin_escalation(
        self,
        user_id: int,
        session_id: int | None,
        report_id: int | None,
        thread_id: str,
        improvement_status: str,
        current_difficulty: str = "",
    ) -> EscalationResult:
        """根据次日反馈中的恶化或没有改善触发安全升级。

        构造相关摘要，必要时创建最小关联记录；当前一次 unchanged 就会触发，并未在此统计连续次数。
        """
        if improvement_status not in ("worsened", "unchanged"):
            return EscalationResult(should_escalate=False)

        status_label = "情况恶化" if improvement_status == "worsened" else "没有改善"
        summary = PrivacySanitizer.build_review_summary(
            current_difficulty=current_difficulty or f"次日反馈：{status_label}",
            risk_trend=f"次日反馈：{status_label}",
            action_plan_status="行动计划仍在进行，未见改善",
        )
        return self._create_escalation(
            user_id, session_id, report_id, thread_id,
            handoff_reason="SUSTAINED_NO_IMPROVEMENT",
            desensitized_summary=summary,
            report_kind="次日反馈",
        )

    def check_screening_escalation(
        self,
        user_id: int,
        session_id: int | None,
        report_id: int | None,
        thread_id: str,
        high_risk_flagged: bool,
        current_difficulty: str = "",
    ) -> EscalationResult:
        """在自愿量表筛查标记需立即关注时触发人工审核。

        high_risk_flagged 为 False 时直接返回；为 True 时整理困境与筛查来源摘要。
        """
        if not high_risk_flagged:
            return EscalationResult(should_escalate=False)

        summary = PrivacySanitizer.build_review_summary(
            current_difficulty=current_difficulty,
            risk_trend="自愿量表筛查：检测到高风险答案",
        )
        return self._create_escalation(
            user_id, session_id, report_id, thread_id,
            handoff_reason="HIGH_RISK_KEYWORD",
            desensitized_summary=summary,
        )

    def user_request_escalation(
        self,
        user_id: int,
        session_id: int | None,
        report_id: int,
        thread_id: str,
        current_difficulty: str = "",
    ) -> EscalationResult:
        """为用户主动请求人工支持创建对应原因的升级结果。

        使用传入会话和记录编号关联审核，困境摘要先经过隐私处理。
        """
        summary = PrivacySanitizer.build_review_summary(
            current_difficulty=current_difficulty,
        risk_trend="用户主动请求人工支持",
        )
        return self._create_escalation(
            user_id, session_id, report_id, thread_id,
            handoff_reason="USER_REQUEST",
            desensitized_summary=summary,
        )

    @staticmethod
    def get_screening_suggestion() -> str:
        """返回自愿量表筛查的固定邀请文字。

        不强制开始量表，也不保存答案或推断量表分数。
        """
        return SCREENING_SUGGESTION


    def _create_escalation(
        self,
        user_id: int,
        session_id: int | None,
        report_id: int | None,
        thread_id: str,
        handoff_reason: str,
        desensitized_summary: str,
        report_kind: str = "对话",
    ) -> EscalationResult:
        """汇总升级原因，并在依赖和会话信息齐全时创建审核记录。

        缺少审核服务或会话信息仍返回需要升级，但 review_id 为空，不能当作已成功入队。
        无关联评估记录时先创建最小记录，再单独保存审核请求。
        """
        if self.review_svc is None:
            return EscalationResult(
                should_escalate=True,
                handoff_reason=handoff_reason,
                desensitized_summary=desensitized_summary,
                user_message=SAFETY_MESSAGE,
            )

        if session_id is None or not thread_id:
            logger.warning(
                "escalation (%s) for user %s has no session/thread; review not created",
                handoff_reason, user_id,
            )
            return EscalationResult(
                should_escalate=True,
                handoff_reason=handoff_reason,
                desensitized_summary=desensitized_summary,
                user_message=SAFETY_MESSAGE,
            )

        if report_id is None:
            report_id = self._ensure_report(user_id, session_id, handoff_reason, report_kind)

        review = self.review_svc.create_with_context(
            session_id=session_id,
            report_id=report_id,
            thread_id=thread_id,
            handoff_reason=handoff_reason,
            desensitized_summary=desensitized_summary,
        )
        return EscalationResult(
            should_escalate=True,
            handoff_reason=handoff_reason,
            desensitized_summary=desensitized_summary,
            user_message=SAFETY_MESSAGE,
            review_id=review.id,
        )

    def _ensure_report(self, user_id: int, session_id: int, handoff_reason: str, report_kind: str) -> int:
        """为非完整模型评估来源的升级建立最小关联记录。

        使用零置信度和说明标注其来源，供审核列表关联展示；提交后返回编号，不伪称模型已评估。
        """
        from app.models.entities import SafetyAssessmentRecord

        report = SafetyAssessmentRecord(
            user_id=user_id,
            session_id=session_id,
            content=handoff_reason,
            intent="CONSULT",
            emotion="NORMAL",
            emotion_score=0.0,
            risk_level="MEDIUM",
            confidence=0.0,
            summary=f"由{report_kind}触发的升级（非对话/量表评估）",
        )
        self.db.add(report)
        self.db.commit()
        self.db.refresh(report)
        return report.id
