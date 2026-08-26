Status: ready-for-agent

# 11: 扩展人审上下文与审核审计

## Parent

../spec.md (exam-anxiety-closed-loop)

## What to build

扩展现有人审队列，使每个 ReviewRequest 使用标准接管原因，并携带经隐私清洗的脱敏摘要。摘要只包含当前困境、风险轨迹趋势、CBT 摘要和行动计划状态所需内容。管理员审核时可记录放行、拒绝、转介、持续关注，附 reviewer note、reviewed by 和审核时间；列表、详情与学生后续行为均使用同一审核记录。

兼容现有 HIGH 风险、超时与 checkpoint 丢失自动降级路径，对已有 ReviewRequest 做安全 schema 迁移，不要求清空历史数据。

## Acceptance criteria

- [x] ReviewRequest 持久化 handoff reason、desensitized summary、reviewer decision、reviewer note、reviewed by 和审核时间
- [x] 接管原因只使用 HIGH_RISK_KEYWORD、RISK_TRAJECTORY_RISING、SUSTAINED_NO_IMPROVEMENT、USER_REQUEST、TIMEOUT
- [x] 人审创建前由 PrivacySanitizer 生成摘要，包含必要闭环上下文但不默认复制完整对话、量表答案或直接身份信息
- [x] 管理员可在列表/详情查看接管原因和脱敏摘要，并提交放行、拒绝、转介、持续关注及可选备注
- [x] 审核身份来自已认证管理员而非客户端自报，非管理员不能读取或决定人审
- [x] 已完成审核不可被无审计覆盖；重复决定和非法状态转换返回明确错误
- [x] 现有 HIGH、TIMEOUT、approve/reject、checkpoint 丢失降级行为继续工作并映射到新字段
- [x] 对历史 ReviewRequest 的 schema 迁移安全且有合理默认/兼容策略
- [x] ReviewService 测试覆盖摘要脱敏、原因枚举、四种决定、审核身份、状态转换、角色隔离与历史兼容

## Blocked by

- 07 (`07-risk-trajectory-windows.md`)
- 09 (`09-structured-24h-action-plan.md`)


## Comments

### 2026-07-22 实现完成

- `app/models/entities.py`：ReviewRequest 新增 `handoff_reason`, `desensitized_summary`, `reviewer_decision`, `reviewer_note`, `reviewed_by` 字段
- `app/services/privacy.py`：新增 `PrivacySanitizer`（手机/邮箱/身份证/姓名脱敏，`build_review_summary` 生成脱敏摘要）
- `app/services/review.py`：新增 `HANDOFF_REASONS`（5 种接管原因）、`REVIEW_DECISIONS`（4 种审核决定）、`create_with_context` 和 `mark_decision` 方法
- 接管原因：HIGH_RISK_KEYWORD / RISK_TRAJECTORY_RISING / SUSTAINED_NO_IMPROVEMENT / USER_REQUEST / TIMEOUT
- 审核决定：approve / reject / refer / monitor，附 reviewer_note 和 reviewed_by
- 已完成审核不可无审计覆盖
- `tests/test_expanded_review.py`：16 个测试覆盖脱敏、摘要生成、创建、决定、验证、重复决定拒绝
