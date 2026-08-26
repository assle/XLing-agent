Status: ready-for-agent

# 06: 待审队列列表 + 辅导员告警

## Parent

../spec.md (langgraph-deep-upgrade)

## What to build

新增 admin 鉴权的待审列表端点，返回 pending 状态的待审消息（含学生消息、风险摘要、emotion label、最近对话上下文、等待时长）。新待审项创建时通过现有 tool queue / email alert 机制通知辅导员（复用 alert 配置或新增 ToolJobKind），不另造通知系统。列表默认按创建时间排序（紧急度排序在 07 做）。

## Acceptance criteria

- [ ] admin 鉴权的 list 端点返回 pending 待审消息
- [ ] 每条待审含学生消息、风险摘要、emotion label、最近对话上下文、等待时长
- [ ] 新 HIGH 待审项创建时触发辅导员告警（通过现有 alert 机制）
- [ ] approve/reject 后该项从 pending 列表移除
- [ ] 测试验证列表内容和告警触发

## Blocked by

- 04（interrupt 触发 + 待审核确认）
