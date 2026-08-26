Status: ready-for-agent

# 04: Interrupt 触发 + 待审核确认

## Parent

../spec.md (langgraph-deep-upgrade)

## What to build

RiskGuardianAgent 评估为 HIGH 时，通过 LangGraph `interrupt()` 中断图执行（停在 CounselorAgent 生成回复之前）。`run()` 的返回区分"已完成"（含 response_messages）和"待审核"两种状态。ChatService 收到"待审核"时发送含危机支持资源的确认消息并关闭 SSE 流，同时创建待审队列实体（关联 session、PsychologicalReport、风险摘要、状态 pending）。日常对话仍走快速路径。Custom runtime 无 interrupt，但 HIGH 同样返回待审核状态。

本 slice 只做中断 + 确认 + 入队，不做 resume（resume 在 05）。

## Acceptance criteria

- [ ] HIGH 风险输入触发 interrupt，run() 返回"待审核"状态（非已完成 response_messages）
- [ ] 学生收到含危机支持资源的确认消息（非 AI 生成回复）
- [ ] 待审队列实体被创建，状态 pending
- [ ] CHAT 输入不触发 interrupt，走 CompanionAgent 正常回复
- [ ] CONSULT/RISK + LOW/MEDIUM 风险不触发 interrupt，走 CounselorAgent 正常回复
- [ ] Custom runtime（LangGraph 不可用时）HIGH 返回待审核状态，不直接生成回复
- [ ] 测试用 mock AiClient 验证以上各路径

## Blocked by

- 01（assessment 单测基线 -- HIGH 判定是人审触发前提，先验证）
- 03（Checkpointer -- interrupt 依赖 checkpointer 持久化中断状态）
