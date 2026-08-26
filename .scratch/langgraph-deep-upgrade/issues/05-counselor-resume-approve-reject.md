Status: ready-for-agent

# 05: 辅导员 resume（批准 + 驳回）

## Parent

../spec.md (langgraph-deep-upgrade)

## What to build

新增 admin 鉴权的审核 API：批准和驳回待审消息。批准时通过 `Command(resume={"approved": True})` 恢复图，CounselorAgent 生成回复并投递给学生；驳回时 `Command(resume={"approved": False})`，发送安全兜底回复（含危机资源联系方式）。待审队列实体状态更新为 approved/rejected。生成的回复存为 ChatMessage，学生可在后续交互获取。

## Acceptance criteria

- [ ] admin 鉴权的 approve 端点：恢复图 -> CounselorAgent 生成回复 -> 存为 ChatMessage
- [ ] admin 鉴权的 reject 端点：恢复图 -> 安全兜底回复（含危机资源）-> 存为 ChatMessage
- [ ] 待审队列实体状态更新为 approved/rejected
- [ ] 批准路径回复内容基于检索知识和风险评估（与正常 CounselorAgent 一致）
- [ ] 驳回路径回复是固定安全兜底（不含 AI 生成内容）
- [ ] 测试用 mock AiClient 验证 approve 和 reject 两条路径

## Blocked by

- 04（interrupt 触发 + 待审核确认）
