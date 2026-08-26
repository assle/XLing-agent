from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.entities import ActionPlan, ActionPlanItem, CheckIn


VALID_STATUSES = {"improved", "unchanged", "worsened"}


class CheckInService:
    """Next-day feedback for action plans (issue 10)."""

    def __init__(self, db: Session):
        self.db = db

    def get_pending_plans(self, user_id: int) -> list[ActionPlan]:
        """Find active plans that need next-day feedback."""
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
            if not self._has_checkin(plan.id)
        ]

    def submit_checkin(
        self,
        user_id: int,
        plan_id: int,
        improvement_status: str,
        notes: str = "",
        item_states: list[dict] | None = None,
    ) -> CheckIn:
        """Submit or update next-day feedback for a plan.

        Idempotent: if feedback already exists for this plan, it's updated.
        """
        if improvement_status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {improvement_status}. Valid: {VALID_STATUSES}")

        plan = self.db.get(ActionPlan, plan_id)
        if plan is None or plan.user_id != user_id:
            raise ValueError("Plan not found or not owned by user")

        # Build items snapshot
        items = sorted(plan.items, key=lambda i: i.order_index)
        if item_states is None:
            item_states = [{"id": i.id, "completed": i.completed} for i in items]
        snapshot = json.dumps(item_states, ensure_ascii=False)

        # Check if feedback already exists (idempotent update)
        existing = self.db.query(CheckIn).filter(CheckIn.plan_id == plan_id).first()
        if existing:
            existing.items_snapshot_json = snapshot
            existing.improvement_status = improvement_status
            existing.notes = notes
            existing.submitted_at = datetime.utcnow()
            self.db.commit()
            self.db.refresh(existing)
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
        return (
            self.db.query(CheckIn)
            .filter(CheckIn.user_id == user_id)
            .order_by(CheckIn.submitted_at.desc())
            .all()
        )

    def get_checkin(self, user_id: int, checkin_id: int) -> CheckIn | None:
        checkin = self.db.get(CheckIn, checkin_id)
        if checkin is None or checkin.user_id != user_id:
            return None
        return checkin

    def to_response(self, checkin: CheckIn) -> dict:
        return {
            "id": checkin.id,
            "planId": checkin.plan_id,
            "improvementStatus": checkin.improvement_status,
            "notes": checkin.notes,
            "itemsSnapshot": json.loads(checkin.items_snapshot_json),
            "submittedAt": checkin.submitted_at.isoformat(),
        }

    def get_by_plan(self, plan_id: int) -> CheckIn | None:
        return self.db.query(CheckIn).filter(CheckIn.plan_id == plan_id).first()

    def _has_checkin(self, plan_id: int) -> bool:
        return self.get_by_plan(plan_id) is not None

    def _update_plan_status(self, plan: ActionPlan, improvement_status: str) -> None:
        """Update plan status based on next-day feedback."""
        if improvement_status == "improved":
            plan.status = "completed"
        elif improvement_status == "unchanged":
            plan.status = "active"  # remains active for adjustment
        elif improvement_status == "worsened":
            plan.status = "active"  # remains active, may trigger escalation
        self.db.commit()
