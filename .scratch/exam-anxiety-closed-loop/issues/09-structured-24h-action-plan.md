Status: ready-for-agent

# 09: 结构化 24 小时行动计划

## Parent

../spec.md (exam-anxiety-closed-loop)

## What to build

当 CBT 四维完成后，基于用户画像、四维摘要和阶段过滤知识生成一份安全、现实的 24 小时行动计划。计划由持久化的 ActionPlan 与有顺序、可独立操作的 ActionPlanItem 组成；用户可以查看活动/历史计划，逐条完成，拒绝或替换不合适条目。结构化生成必须通过 schema 校验，不能从一段自由文本反向猜条目。

这一 slice 贯穿 agent runtime、持久化、用户资源 API 与学生端交互；活动计划离开页面后仍可恢复，已完成记录不会被替换操作静默改写。

## Acceptance criteria

- [x] 只有当前活动闭环的四个 CBT 维度齐全后才生成行动计划
- [x] 计划保存用户、来源会话/闭环、创建时间、24 小时目标窗口、状态及有序条目
- [x] 每个条目小而具体、安全、与备考阶段及 CBT 摘要相关，并通过 Structured Output schema 校验
- [x] 用户可查看自己的活动和历史计划，不能读取或操作他人计划
- [x] 用户可逐条标记完成，完成时间持久化，刷新或重新登录后状态不丢失
- [x] 用户可拒绝或替换未完成的不适合条目；已完成条目及历史结果不会被静默覆盖
- [x] 模型结构化生成失败时进入安全 fallback，不保存空计划、危险条目或半解析计划
- [x] ActionPlanService 与 runtime 测试覆盖生成、归属、排序、逐条完成、替换、跨请求恢复和失败 fallback

## Blocked by

- 06 (`06-exam-stage-rag-filtering.md`)
- 08 (`08-cbt-structured-questioning.md`)


## Comments

### 2026-07-22 实现完成

- `app/models/entities.py`：新增 `ActionPlan`（user_id, session_id, status, target_window_hours）和 `ActionPlanItem`（plan_id, content, order_index, completed, completed_at）
- `app/services/action_plan.py`：新增 `ActionPlanService`（generate_plan, get_plan, list_plans, mark_item_completed, replace_item, to_response）
  - `ActionPlanSchema` Pydantic schema 约束生成结构
  - LLM 生成失败时使用安全 fallback（3 条保守行动条目）
  - 已完成条目不可替换
- `app/api/routes.py`：新增 `GET /api/action-plans`, `GET /api/action-plans/{id}`, `POST /api/action-plans/items/{id}/complete`, `PUT /api/action-plans/items/{id}`
- `tests/test_action_plan.py`：13 个测试覆盖生成、fallback、CRUD、完成/替换、用户隔离、API
