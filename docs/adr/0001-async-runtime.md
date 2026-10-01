# 1. 异步化 Agent Runtime

日期：2026-07-15

## 状态

Accepted

## 背景

同步的对话准备流程曾在异步流式接口中直接执行记忆摘要、消息分流、查询改写和风险评估的模型请求，阻塞事件循环并推迟首个回复片段。

## 决策

- `LangGraphAgentRuntimeService.run` 和 `resume` 通过 `await graph.ainvoke(...)` 执行唯一的 LangGraph 流程。
- 记忆摘要、消息分流和查询改写使用 `AiClient.acomplete`；安全风险评估使用 `PsychologicalAssessmentService.aassess`。
- `ChatService.prepare` 和流式聊天入口采用异步调用，审核恢复也异步读取持久检查点。
- `AiClient.complete` 和同步评估入口继续供同步业务服务与离线调用方使用。四维信息提取和行动计划生成沿用各自业务服务，不改变其同步接口。
- 图节点返回明确字段更新；业务结果和依赖契约不承担调度，原生状态与唯一运行器由 ADR-0012 确定。

## 后果

记忆摘要、分流、查询和评估的网络等待不阻塞异步事件循环。检查点后端必须支持异步调用，默认采用官方 `AsyncSqliteSaver`。四维信息提取和行动计划生成仍包含同步业务调用，不能据此宣称所有阶段均无阻塞。

## 相关

- [ADR-0002](0002-checkpointer-memorysaver.md)
- [ADR-0012](0012-langgraph-runtime-and-local-diagnostics.md)
