from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app.models.entities import ActionPlan, ActionPlanItem
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structured output schema (issue 09)
# ---------------------------------------------------------------------------

class ActionPlanItemSchema(BaseModel):
    content: str = Field(min_length=1, max_length=500)
    order: int = Field(ge=0)


class ActionPlanSchema(BaseModel):
    items: list[ActionPlanItemSchema] = Field(min_length=1, max_length=6)


# ---------------------------------------------------------------------------
# Fallback plan (safe, conservative)
# ---------------------------------------------------------------------------

FALLBACK_ITEMS = [
    "今晚把最担心的一件事写在纸上，只选一个最小步骤明天先做",
    "睡前 30 分钟把手机放远，用缓慢呼吸帮身体放松",
    "明天固定一个 25 分钟专注时段，只做最重要的一件事",
]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class ActionPlanService:
    """Structured 24h action plan generation and management (issue 09)."""

    def __init__(self, db: Session, ai: AiClient | None = None):
        self.db = db
        self.ai = ai

    def generate_plan(
        self,
        user_id: int,
        session_id: int | None,
        cbt_summary: str = "",
        exam_stage: str = "",
    ) -> ActionPlan:
        """Generate a 24h action plan after four-part questioning.

        Uses LLM structured output; falls back to safe default items on failure.
        Only call this when all four aspects are complete.
        """
        items = self._generate_items(cbt_summary, exam_stage)
        plan = ActionPlan(
            user_id=user_id,
            session_id=session_id,
            status="active",
            target_window_hours=24,
        )
        self.db.add(plan)
        self.db.flush()
        for index, content in enumerate(items):
            self.db.add(ActionPlanItem(
                plan_id=plan.id,
                content=content,
                order_index=index,
            ))
        self.db.commit()
        self.db.refresh(plan)
        return plan

    def get_plan(self, user_id: int, plan_id: int) -> ActionPlan | None:
        plan = self.db.get(ActionPlan, plan_id)
        if plan is None or plan.user_id != user_id:
            return None
        return plan

    def list_plans(self, user_id: int) -> list[ActionPlan]:
        return (
            self.db.query(ActionPlan)
            .filter(ActionPlan.user_id == user_id)
            .order_by(ActionPlan.created_at.desc())
            .all()
        )

    def mark_item_completed(self, user_id: int, item_id: int) -> ActionPlanItem | None:
        item = self._get_owned_item(user_id, item_id)
        if item is None:
            return None
        item.completed = True
        item.completed_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(item)
        return item

    def replace_item(self, user_id: int, item_id: int, new_content: str) -> ActionPlanItem | None:
        """Replace an uncompleted item. Completed items cannot be replaced."""
        item = self._get_owned_item(user_id, item_id)
        if item is None or item.completed:
            return None
        item.content = new_content.strip()
        self.db.commit()
        self.db.refresh(item)
        return item

    def to_response(self, plan: ActionPlan) -> dict:
        items = sorted(plan.items, key=lambda i: i.order_index)
        return {
            "id": plan.id,
            "status": plan.status,
            "targetWindowHours": plan.target_window_hours,
            "createdAt": plan.created_at.isoformat(),
            "items": [
                {
                    "id": item.id,
                    "content": item.content,
                    "order": item.order_index,
                    "completed": item.completed,
                    "completedAt": item.completed_at.isoformat() if item.completed_at else None,
                }
                for item in items
            ],
        }

    # ------------------------------------------------------------------
    # LLM generation
    # ------------------------------------------------------------------

    def _generate_items(self, cbt_summary: str, exam_stage: str) -> list[str]:
        if not self.ai:
            return FALLBACK_ITEMS.copy()
        try:
            messages = self._generation_prompt(cbt_summary, exam_stage)
            raw = self.ai.complete(messages)
            return self._parse_items(raw)
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning("Action plan generation failed, using fallback: %s", exc)
            return FALLBACK_ITEMS.copy()
        except Exception as exc:
            logger.warning("Action plan generation error, using fallback: %s", exc)
            return FALLBACK_ITEMS.copy()

    def _generation_prompt(self, cbt_summary: str, exam_stage: str) -> list[AiMessage]:
        stage_context = f"备考阶段：{exam_stage}\n" if exam_stage else ""
        return [
            AiMessage(role="system", content=(
                "你是一个心理行动规划助手。基于学生的认知行为四维追问摘要，生成 3-5 个小而具体、"
                "安全、可操作的 24 小时行动计划条目。只返回严格 JSON："
                '{"items":[{"content":"具体行动描述","order":0}]}'
                "\n每个条目应小而具体、安全，并与备考阶段和四维追问摘要相关。"
            )),
            AiMessage(role="user", content=(
                f"{stage_context}认知行为四维追问摘要：\n{cbt_summary}"
            )),
        ]

    @staticmethod
    def _parse_items(raw: str) -> list[str]:
        start = raw.find("{")
        end = raw.rfind("}")
        json_str = raw[start:end + 1] if start >= 0 and end > start else raw
        data = json.loads(json_str)
        schema = ActionPlanSchema(**data)
        sorted_items = sorted(schema.items, key=lambda i: i.order)
        return [item.content for item in sorted_items]

    def _get_owned_item(self, user_id: int, item_id: int) -> ActionPlanItem | None:
        item = self.db.get(ActionPlanItem, item_id)
        if item is None:
            return None
        plan = self.db.get(ActionPlan, item.plan_id)
        if plan is None or plan.user_id != user_id:
            return None
        return item
