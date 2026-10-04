from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.core.time import utc_isoformat, utc_now
from app.models.entities import ActionPlan, CheckIn

VALID_STATUSES = {"improved", "unchanged", "worsened"}


class CheckInService:
    """保存和读取行动计划的次日反馈，并据此更新计划状态。"""

    def __init__(self, db: Session):
        """保存用于查询和提交次日反馈的数据库会话。

        初始化不执行查询，连接的关闭由调用方负责。
        """
        self.db = db

    def get_pending_plans(self, user_id: int) -> list[ActionPlan]:
        """查找指定用户仍为 active 且尚无反馈记录的计划。

        按创建时间倒序返回；当前实现不按创建后是否满 24 小时过滤。
        """
        plans = (
            self.db.query(ActionPlan)
            .filter(ActionPlan.user_id == user_id)
            .filter(ActionPlan.status == "active")
            .order_by(ActionPlan.created_at.desc())
            .all()
        )
        return [
            plan
            for plan in plans
            if self.get_by_plan(plan.id) is None
        ]

    def submit_checkin(
        self,
        user_id: int,
        plan_id: int,
        improvement_status: str,
        notes: str = "",
        item_states: list[dict] | None = None,
    ) -> CheckIn:
        """创建或更新某份自有行动计划的次日反馈。

        验证改善状态和计划归属，再保存逐项完成情况的快照；未传 item_states 时读取计划当前状态。
        已有反馈会被更新而非新增；反馈保存后另行提交计划状态，因此两步不是一次统一提交。
        """
        # 先拒绝未知状态，后续计划状态转换才不会落入未定义情况。
        if improvement_status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {improvement_status}. Valid: {VALID_STATUSES}")

        plan = self.db.get(ActionPlan, plan_id)
        if plan is None or plan.user_id != user_id:
            raise ValueError("Plan not found or not owned by user")

        # 保存提交当下的行动项完成情况快照。
        # 按原计划顺序生成完成情况快照，便于之后理解反馈对应的行动项。
        items = sorted(plan.items, key=lambda i: i.order_index)
        # 未提供逐项快照时，使用提交当下计划中已经保存的完成状态。
        if item_states is None:
            item_states = [{"id": i.id, "completed": i.completed} for i in items]
        snapshot = json.dumps(item_states, ensure_ascii=False)

        # 查询同一计划已有反馈；重复提交更新原记录。
        existing = self.db.query(CheckIn).filter(CheckIn.plan_id == plan_id).first()
        # 同一计划最多保留一份反馈，重复提交更新该记录和提交时间。
        if existing:
            existing.items_snapshot_json = snapshot
            existing.improvement_status = improvement_status
            existing.notes = notes
            existing.submitted_at = utc_now()
            self.db.commit()
            self.db.refresh(existing)
            # 反馈记录此前已经提交；计划状态在另一个保存步骤中更新。
            self._update_plan_status(plan, improvement_status)
            return existing

        checkin = CheckIn(
            plan_id=plan_id,
            user_id=user_id,
            items_snapshot_json=snapshot,
            improvement_status=improvement_status,
            notes=notes,
        )
        self.db.add(checkin)
        self.db.commit()
        self.db.refresh(checkin)
        self._update_plan_status(plan, improvement_status)
        return checkin

    def list_checkins(self, user_id: int) -> list[CheckIn]:
        """按最近提交时间优先列出指定用户的反馈记录。

        返回数据库对象，字段转换由 to_response 统一处理。
        """
        return (
            self.db.query(CheckIn)
            .filter(CheckIn.user_id == user_id)
            .order_by(CheckIn.submitted_at.desc())
            .all()
        )


    def to_response(self, checkin: CheckIn) -> dict:
        """把反馈记录及保存的行动项快照转换为接口字典。

        将快照文本还原成列表，并把提交时间转换为标准文本；损坏的快照解析错误会向上传递。
        """
        return {
            "id": checkin.id,
            "planId": checkin.plan_id,
            "improvementStatus": checkin.improvement_status,
            "notes": checkin.notes,
            "itemsSnapshot": json.loads(checkin.items_snapshot_json),
            "submittedAt": utc_isoformat(checkin.submitted_at),
        }

    def get_by_plan(self, plan_id: int) -> CheckIn | None:
        """查询一份行动计划已有的反馈记录。

        只按计划编号查找，不校验用户归属；需要访问控制的调用方应先验证计划所有者。
        """
        return self.db.query(CheckIn).filter(CheckIn.plan_id == plan_id).first()


    def _update_plan_status(self, plan: ActionPlan, improvement_status: str) -> None:
        """根据改善情况更新计划状态并确认保存。

        改善时结束计划；无变化或恶化时保留 active，供后续调整或安全升级处理。
        """
        if improvement_status == "improved":
            plan.status = "completed"
        elif improvement_status == "unchanged":
            plan.status = "active"  # 保持进行中，供后续调整。
        elif improvement_status == "worsened":
            plan.status = "active"  # 保持进行中，其他入口可继续检查安全升级。
        self.db.commit()
