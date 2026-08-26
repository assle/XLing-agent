Status: ready-for-human

# 07: 审核紧急度排序 + 超时升级

## Parent

../spec.md (langgraph-deep-upgrade)

## What to build

待审列表按紧急度排序（risk level + 等待时长加权）。超时未审核的待审项升级。本 slice 标 HITL 因超时时长和升级动作是产品决策，需人确认后再实现。

待人确认的决策：
- 超时时长定为多少？（如 15 分钟 / 30 分钟）
- 超时后做什么？（重新告警 / 升级到更高优先级通道 / 自动放行兜底回复 / 其他）

## Acceptance criteria

- [x] 待审列表按紧急度排序（HIGH 优先于 MEDIUM，同 risk 按等待时长降序）
- [x] 超时升级策略经人确认后实现
- [x] 超时后执行升级动作（自动兜底）
- [x] 测试验证排序和超时升级

## Blocked by

- 06（待审队列列表 + 辅导员告警）

## Comments

**2026-07-15 已实现。** 用户确认决策：超时 15 分钟，升级动作 = 自动兜底（发送固定安全回复）。

实现细节：
- `review_timeout_minutes: int = 15` 配置项（config.py）
- `ReviewService.escalate_timed_out()`：pending 且超过 15 分钟的待审项自动保存 fallback ChatMessage，status 改为 `escalated`
- `list_pending` 按紧急度排序：risk level 降序 + 等待时长降序
- `GET /api/admin/reviews` 列表前先调用 `escalate_timed_out`（懒升级）
- 6 个新测试覆盖排序和超时兜底（test_review.py）

提交：`feat: review urgency ordering + timeout auto-fallback (issue 07)`
