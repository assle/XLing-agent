# 1. 异步化 Agent Runtime

日期：2026-07-15

## 状态

Accepted

## 背景

`ChatService.prepare()` 是同步方法，在 async 的 `stream_chat` 里被直接调用。prepare 阶段最多跑 4 次同步 LLM 调用（记忆摘要、意图分类、查询改写、风险评估），每次用 `httpx.post` 同步阻塞 HTTP。这些调用全跑完才开始吐第一个 token。

问题：
- **阻塞事件循环**：同步 `httpx.post` 在 async 事件循环里执行，会卡住整个服务。一个用户的请求冻住所有并发用户。
- **首 token 延迟高**：support 路径最多 4 次 LLM 往返 + DB + Redis 全跑完才出第一个字。

## 决策

全链路异步化：
- `AiClient` 新增 `async def acomplete`（用 `httpx.AsyncClient`，复用已有 `stream` 的 async 模式）
- `AgentRuntimeService.run` 改 `async def`，所有 LLM 调用用 `await self.ai.acomplete(...)`
- `LangGraphAgentRuntimeService.run` 用 `await self.graph.ainvoke(...)`
- `ChatService.prepare` 改 `async def`，`stream_chat` 用 `await`
- 同步 `complete` / `assess` 保留，供迁移期和非 async 调用方使用

`PsychologicalAssessmentService` 提取 `_parse_assessment` 共享解析逻辑，新增 `async def aassess`（用 `acomplete`），同步 `assess` 不变。

## 后果

正面：
- 事件循环不再阻塞，并发请求互不冻结
- 首 token 延迟降低（prepare 不阻塞，流式能更快开始）
- 面试核心卖点："我发现首 token 延迟高的根因是 4 次同步 LLM 调用阻塞了事件循环，我把 runtime 改成 async"

负面：
- 所有 agent 方法必须 async（cascading change，run/memory_agent/supervisor_agent/... 全改）
- Checkpointer 必须支持 async（见 [ADR-0002](0002-checkpointer-memorysaver.md)）
- 测试需用 `asyncio.run` 包装

## 相关

- 研究报告 §B（异步化与首 token 延迟）
- Issue 02（async end-to-end）
- 衍生约束：SqliteSaver（sync）不支持 ainvoke -> 见 [ADR-0002](0002-checkpointer-memorysaver.md)
