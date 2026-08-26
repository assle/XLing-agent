Status: ready-for-agent

# 10: 次日 check-in 与正常闭环完成/调整

## Parent

../spec.md (exam-anxiety-closed-loop)

## What to build

为活动行动计划提供应用内次日 check-in。用户回到应用时能直接看到待 check-in 计划，报告条目执行情况、焦虑改善/不变/恶化及可选说明。完成或改善可结束当前闭环；未完成时采用非评判措辞，允许保留、缩小或替换下一步。产品不以精确 24 小时硬拦截用户，但要保留目标窗口和实际 check-in 时间。

本 slice 交付正常完成与协作调整路径；由风险轨迹触发的量表建议和人审升级在 issue 12 接通。

## Acceptance criteria

- [x] 有活动行动计划的用户能在应用内看到待 check-in 入口，离开后重新登录仍可继续
- [x] check-in 关联唯一计划，保存条目执行快照、改善/不变/恶化、自述说明和提交时间
- [x] 用户可在目标 24 小时前后提交，系统记录实际时间但不因时间偏差拒绝
- [x] 完成或自报改善时，活动闭环进入完成状态且保留可查看历史
- [x] 未完成或不变时，界面采用非评判措辞并允许保留、缩小或替换未完成条目
- [x] 同一活动计划的重复提交具有明确、可测试的幂等/更新语义，不制造互相矛盾的活动闭环
- [x] 用户只能查看和提交自己的 check-in，管理员能力不绕过 service 归属规则
- [x] Agent runtime 与 ActionPlanService 测试覆盖完成、改善、未完成调整、时间边界、重复提交和跨请求恢复

## Blocked by

- 09 (`09-structured-24h-action-plan.md`)


## Comments

### 2026-07-22 实现完成

- `app/models/entities.py`：新增 `CheckIn` 模型（plan_id, user_id, items_snapshot_json, improvement_status, notes, submitted_at）
- `app/services/checkin.py`：新增 `CheckInService`（get_pending_plans, submit_checkin, list_checkins, get_checkin）
  - 改善 -> plan.status=completed；不变/恶化 -> 保持 active
  - 幂等提交：同一 plan 的重复 check-in 更新而非新建
  - 非评判措辞支持（unchanged/worsened 保持 active 允许调整）
- `app/api/routes.py`：新增 `GET /api/check-ins/pending`, `POST /api/check-ins`, `GET /api/check-ins`
- `tests/test_checkin.py`：13 个测试覆盖 pending 检测、提交、幂等、状态更新、用户隔离、API
