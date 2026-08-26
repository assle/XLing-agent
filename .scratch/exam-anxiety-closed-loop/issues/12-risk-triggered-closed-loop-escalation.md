Status: ready-for-agent

# 12: 风险触发量表与恶化升级闭环

## Parent

../spec.md (exam-anxiety-closed-loop)

## What to build

把此前独立能力接成闭环的升级路径：风险轨迹持续上升时向用户建议自愿 PHQ-9/GAD-7，而不是自动作答或强迫筛查；用户拒绝后仍可聊天，但既有安全分流继续有效。次日 check-in 恶化或持续无改善时，结合会话内 3 条与跨会话 7 天轨迹决定安全跟进或创建人审，并分别使用 RISK_TRAJECTORY_RISING / SUSTAINED_NO_IMPROVEMENT。用户主动要求真人时以 USER_REQUEST 直接接管。

这一 tracer bullet 要用一条完整 agent runtime 流程证明“进入 -> 风险分流 -> CBT 追问 -> 行动计划 -> 次日 check-in -> 升级或完成”两种结局均可达。

## Acceptance criteria

- [x] 风险轨迹达到配置阈值时提供自愿量表建议，未同意前不创建答卷、答案或分数
- [x] 用户拒绝或中止风险触发筛查后仍能聊天，系统不会反复在同一触发点骚扰式建议
- [x] 风险触发筛查中的高风险答案使用 HIGH_RISK_KEYWORD 或等价明确安全原因进入人审
- [x] check-in 恶化或连续无改善能产生风险轨迹点，并按阈值创建 SUSTAINED_NO_IMPROVEMENT 人审
- [x] 单条未达 HIGH 但趋势上升达到阈值时创建 RISK_TRAJECTORY_RISING 人审
- [x] 用户明确要求真人支持时无需等待轨迹阈值，创建 USER_REQUEST 人审
- [x] 所有升级都生成脱敏摘要并先向用户给出适当安全/等待说明，不暴露后台风险标签
- [x] 完整 runtime 测试覆盖正常完成、未改善调整、风险触发筛查同意/拒绝、轨迹升级、持续无改善和用户主动接管

## Blocked by

- 05 (`05-voluntary-explicit-screening.md`)
- 07 (`07-risk-trajectory-windows.md`)
- 10 (`10-next-day-check-in.md`)
- 11 (`11-expanded-human-review.md`)


## Comments

### 2026-07-22 实现完成

- `app/services/escalation.py`：新增 `EscalationService` 连接风险轨迹、筛查、check-in 和人审
  - `check_trajectory_escalation`：轨迹上升 -> RISK_TRAJECTORY_RISING 人审（HIGH 由关键词路径处理）
  - `check_checkin_escalation`：恶化/无改善 -> SUSTAINED_NO_IMPROVEMENT 人审
  - `check_screening_escalation`：筛查高风险答案 -> HIGH_RISK_KEYWORD 人审
  - `user_request_escalation`：用户主动请求 -> USER_REQUEST 人审
  - 所有升级生成脱敏摘要 + 安全说明（含热线号码）
  - 量表建议为自愿性消息，不强制
- `tests/test_escalation.py`：12 个测试覆盖 4 种升级路径、不升级路径、安全消息、量表建议
