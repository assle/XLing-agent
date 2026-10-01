from __future__ import annotations

import json
from datetime import timedelta

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app.core import diagnostics
from app.core.time import utc_now
from app.models.entities import ActionPlan, ActionPlanItem
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient

# ---------------------------------------------------------------------------
# 行动计划的结构化输出约束。
# ---------------------------------------------------------------------------

class ActionPlanItemSchema(BaseModel):
    content: str = Field(min_length=1, max_length=500)
    order: int = Field(ge=0)


class ActionPlanSchema(BaseModel):
    items: list[ActionPlanItemSchema] = Field(min_length=1, max_length=6)


# ---------------------------------------------------------------------------
# 模型不可用时采用的保守行动项。
# ---------------------------------------------------------------------------

FALLBACK_ITEMS = [
    "今晚把最担心的一件事写在纸上，只选一个最小步骤明天先做",
    "睡前 30 分钟把手机放远，用缓慢呼吸帮身体放松",
    "明天固定一个 25 分钟专注时段，只做最重要的一件事",
]


# ---------------------------------------------------------------------------
# 服务实现。
# ---------------------------------------------------------------------------

class ActionPlanService:
    """生成和管理与用户当前处境相关的 24 小时行动计划。"""

    def __init__(self, db: Session, ai: AiClient | None = None):
        """保存数据库会话和可选模型客户端。

        不传模型时生成流程会使用保守的默认行动项；初始化本身不写入数据。
        """
        self.db = db
        self.ai = ai

    def generate_plan(
        self,
        user_id: int,
        session_id: int | None,
        cbt_summary: str = "",
        *,
        commit: bool = True,
    ) -> ActionPlan:
        """根据四维追问摘要生成并保存一份 24 小时行动计划。

        user_id 和 session_id 确定归属，cbt_summary 提供生成背景；调用方负责确认四个方面已完成。
        commit 为 False 时只写入未提交事务，允许外层统一确认保存；返回带数据库编号的计划对象。
        """
        items = self._generate_items(cbt_summary)
        plan = ActionPlan(
            user_id=user_id,
            session_id=session_id,
            status="active",
            target_window_hours=24,
        )
        self.db.add(plan)
        # 先取得计划编号，后续行动项才能通过 plan_id 与这份计划关联。
        self.db.flush()
        # 为每个行动项保存独立行和顺序编号，界面完成或替换时可定位单项。
        for index, content in enumerate(items):
            self.db.add(ActionPlanItem(
                plan_id=plan.id,
                content=content,
                order_index=index,
            ))
        # 允许外层决定是否统一提交；仅 flush 不代表数据已确认保存。
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        self.db.refresh(plan)
        return plan

    def get_plan(self, user_id: int, plan_id: int) -> ActionPlan | None:
        """按编号查找属于指定用户的行动计划。

        不存在或归属不匹配时返回 None，避免上层读取其他用户的计划。
        """
        plan = self.db.get(ActionPlan, plan_id)
        if plan is None or plan.user_id != user_id:
            return None
        return plan

    def list_plans(self, user_id: int) -> list[ActionPlan]:
        """按创建时间从新到旧列出指定用户的行动计划。

        返回数据库对象列表，不限定计划状态，也不修改记录。
        """
        return (
            self.db.query(ActionPlan)
            .filter(ActionPlan.user_id == user_id)
            .order_by(ActionPlan.created_at.desc())
            .all()
        )

    def mark_item_completed(self, user_id: int, item_id: int) -> ActionPlanItem | None:
        """记录一个自有行动项的完成状态和当前完成时间。

        找不到自有行动项时返回 None；找到则提交并刷新。再次调用也会更新完成时间。
        """
        item = self._get_owned_item(user_id, item_id)
        if item is None:
            return None
        item.completed = True
        item.completed_at = utc_now()
        self.db.commit()
        self.db.refresh(item)
        return item

    def replace_item(self, user_id: int, item_id: int, new_content: str) -> ActionPlanItem | None:
        """替换尚未完成的自有行动项，去掉文本两端空白。

        找不到或已经完成时返回 None；成功时确认保存并返回刷新后的行动项。
        """
        item = self._get_owned_item(user_id, item_id)
        # 已经完成的行动内容应保留，不能用替换操作改写其含义。
        if item is None or item.completed:
            return None
        item.content = new_content.strip()
        self.db.commit()
        self.db.refresh(item)
        return item

    def to_response(self, plan: ActionPlan) -> dict:
        """整理计划及行动项，计算建议反馈时间并输出网页字段。

        匿名排序函数使用 order_index 保持行动顺序；反馈可用标志只依据 active 状态，不检查是否已到建议时间。
        """
        # 按数据库行动顺序排列，保持面板条目顺序稳定。
        items = sorted(plan.items, key=lambda i: i.order_index)
        feedback_due_at = plan.created_at + timedelta(hours=plan.target_window_hours)
        return {
            "id": plan.id,
            "status": plan.status,
            "targetWindowHours": plan.target_window_hours,
            "createdAt": plan.created_at.isoformat(),
            "feedbackDueAt": feedback_due_at.isoformat(),
            "feedbackAvailable": plan.status == "active",
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
    # 模型生成与输出解析。
    # ------------------------------------------------------------------

    def _generate_items(self, cbt_summary: str) -> list[str]:
        """尝试由模型生成行动项，在不可用或输出解析失败时使用默认项。

        cbt_summary 是四维追问摘要；返回独立的文本列表，默认列表通过复制避免被后续修改污染。
        """
        with diagnostics.stage("action_plan.generate"):
            if not self.ai:
                return FALLBACK_ITEMS.copy()
            try:
                messages = self._generation_prompt(cbt_summary)
                raw = self.ai.complete(messages)
                return self._parse_items(raw)
            # 输出格式或字段不满足约定时使用安全默认条目，不保存未经校验的模型结构。
            except (json.JSONDecodeError, ValidationError) as exc:
                diagnostics.degraded("action_plan.generate", "invalid_action_plan", exc)
                return FALLBACK_ITEMS.copy()
            except Exception as exc:
                diagnostics.degraded("action_plan.generate", "action_plan_model_unavailable", exc)
                return FALLBACK_ITEMS.copy()

    # 把四维追问摘要组装成行动计划生成请求。
    # 返回系统要求与用户背景两条消息，只要求模型给出约定结构；此处不调用模型。
    # 本函数源码参与提示词版本计算，中文说明放在函数外以保持其指纹不变。
    def _generation_prompt(self, cbt_summary: str) -> list[AiMessage]:
        stage_context = ""
        return [
            AiMessage(role="system", content=(
                "你是一个心理行动规划助手。基于用户的认知行为四维追问摘要，生成 3-5 个小而具体、"
                "安全、可操作的 24 小时行动计划条目。只返回严格 JSON："
                '{"items":[{"content":"具体行动描述","order":0}]}'
                "\n每个条目应小而具体、安全，并与用户当前处境和四维追问摘要相关。"
            )),
            AiMessage(role="user", content=(
                f"{stage_context}认知行为四维追问摘要：\n{cbt_summary}"
            )),
        ]

    @staticmethod
    def _parse_items(raw: str) -> list[str]:
        """从模型回复中提取对象文本，校验条目结构并按顺序返回行动内容。

        允许对象前后夹有其他文本；内容或条数不满足约束时抛出解析或校验异常，由生成入口负责回退。
        """
        start = raw.find("{")
        end = raw.rfind("}")
        json_str = raw[start:end + 1] if start >= 0 and end > start else raw
        data = json.loads(json_str)
        # 先验证条数、正文长度和顺序字段，再提取可保存的文本。
        schema = ActionPlanSchema(**data)
        # 按模型给出的顺序字段排序，再提取行动内容。
        sorted_items = sorted(schema.items, key=lambda i: i.order)
        return [item.content for item in sorted_items]

    def _get_owned_item(self, user_id: int, item_id: int) -> ActionPlanItem | None:
        """通过行动项所属计划验证用户归属。

        行动项、计划缺失或计划属于其他用户均返回 None；本函数不检查是否已完成。
        """
        item = self.db.get(ActionPlanItem, item_id)
        if item is None:
            return None
        plan = self.db.get(ActionPlan, item.plan_id)
        if plan is None or plan.user_id != user_id:
            return None
        return item
