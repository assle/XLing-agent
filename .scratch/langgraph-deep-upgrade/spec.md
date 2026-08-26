Status: ready-for-agent

# PRD: LangGraph 深度改造（Checkpointer + 人审中断 + 异步化）

> 对应研究报告 `learn/research/0001-upgrade-and-interview-packaging.md` 的 §A.2 + §A.3 + §B（2026-07-15 已核实代码事实）。

## Problem Statement

Xling 是一个面向学生的校园心理关怀多 Agent 系统。当前存在三个问题：

1. **高风险消息缺少人工兜底**：当 `RiskLevel.HIGH`（如自杀、自残信号）被 `RiskGuardianAgent` 识别后，系统直接让 `CounselorAgent` 用 LLM 生成回复并发送给学生。在心理安全场景下，没有真人介入的纯 AI 回复是不可接受的安全缺口--危机消息应当先经辅导员审核。

2. **首 token 延迟高、阻塞事件循环**：`ChatService.prepare()` 是同步方法，在 async 的 `stream_chat` 里被直接调用。prepare 阶段最多跑 4 次同步 LLM 往返（记忆摘要、意图分类、查询改写、风险评估）加 DB/Redis 操作，全部跑完才开始吐第一个 token。同步的 `httpx.post` 卡住整个事件循环，一个用户的请求会冻住所有并发用户。

3. **Agent 状态不可恢复**：服务崩溃后，只有原始消息历史（Redis/MySQL）留存，Agent 的意图判断、风险评估、检索结果、回复策略全部丢失，无法从崩溃点恢复对话工作流。

## Solution

将当前"穿了件 LangGraph 外衣的状态机"改造为真正的 LangGraph 实现，交付三件事：

1. **高风险人审中断**：`RiskGuardianAgent` 评估为 `RiskLevel.HIGH` 时，通过 LangGraph `interrupt()` 中断图执行（停在 `CounselorAgent` 生成回复之前）。学生立即收到含危机资源的确认消息；消息进入待审队列。辅导员审核后通过 `Command(resume=...)` 恢复图：批准则 `CounselorAgent` 生成回复并发送，驳回则发送安全兜底回复。

2. **全链路异步化**：`AiClient` 增加异步 `acomplete`（复用 `stream` 已有的 `httpx.AsyncClient`）；`AgentRuntimeService.run` 改为 `async`；LangGraph 用 `ainvoke` 执行。prepare 阶段不再阻塞事件循环，首 token 更快到达学生，并发用户互不阻塞。

3. **Checkpointer 会话状态持久化**：`graph.compile(checkpointer=...)` 以 `session.public_id` 作为 `thread_id`，每轮自动快照 Agent 状态。崩溃后可恢复对话工作流，并支持 time travel 调试。Redis 短期记忆退化为消息缓存，Checkpointer 成为 Agent 状态的唯一事实源。

## User Stories

### 人审中断（interrupt）

1. As a student, I want my high-risk message to be held for counselor review before any AI response is sent, so that a human ensures I get appropriate support in a crisis.
2. As a student, when my message is under review, I want to immediately receive an acknowledgment with crisis support resources, so that I have access to help while waiting.
3. As a counselor, I want to see a list of messages pending review, so that I can triage them.
4. As a counselor, I want to see the student's message, risk assessment summary, emotion label, and recent conversation history, so that I can make an informed review decision.
5. As a counselor, I want to approve a pending review, so that the system generates and delivers an appropriate response to the student.
6. As a counselor, I want to reject a pending review, so that the student receives a safe fallback response instead of an inappropriate AI reply.
7. As a counselor, I want pending reviews ordered by urgency (risk level and time waiting), so that the most critical messages are addressed first.
8. As a counselor, I want to be alerted when a high-risk message enters the review queue, so that I can respond promptly.
9. As a student, when my message is approved, I want to receive the AI-generated response, so that the conversation continues.
10. As a student, when my message is rejected, I want to receive a compassionate fallback response with crisis resource contacts, so that I am not left without support.
11. As a counselor, I want only HIGH-risk messages to enter the review queue, so that LOW and MEDIUM risk messages are handled automatically by the AI.
12. As a counselor, I want the review API to require admin authentication, so that only authorized counselors can approve or reject messages.

### 异步化与延迟（async）

13. As a student, I want to see the first token of my response as quickly as possible, so that I know the system is working.
14. As a student, when multiple students use the system concurrently, I want the system to stay responsive, so that one person's request does not freeze the service for everyone.
15. As a developer, I want the LLM client to expose an async completion method, so that LLM calls do not block the async event loop.
16. As a developer, I want the agent runtime to execute asynchronously, so that it integrates with FastAPI's async request handling.
17. As a developer, I want the LangGraph graph to execute via ainvoke, so that graph execution does not block the event loop.

### Checkpointer 与状态持久化

18. As a student, if the service restarts mid-conversation, I want the agent's understanding of our conversation to persist, so that the conversation continues smoothly.
19. As a developer, I want each conversation turn to be checkpointed, so that I can inspect the agent state at each step for debugging.
20. As a developer, I want to resume a conversation from a previous checkpoint (time travel), so that I can reproduce and diagnose issues.
21. As a developer, I want the checkpointer keyed by session public_id, so that each conversation's state is isolated.
22. As a developer, I want the Redis short-term memory demoted to a message cache, so that the checkpointer is the single source of truth for agent state and there is no dual-source-of-truth confusion.

### 集成与边界

23. As a student, when I send a normal CHAT message (not high-risk), I want my experience to be unchanged, so that the review infrastructure does not slow down normal conversations.
24. As a developer, I want the custom runtime (fallback when LangGraph is unavailable) to also be async, so that the latency improvement applies regardless of framework.
25. As a developer, I want the custom runtime to return a pending-review result for HIGH risk even without an interrupt, so that the fallback path keeps the same safety boundary without a checkpointer.

## Implementation Decisions

- **AiClient 异步接口**：新增 `async def acomplete`，用 `httpx.AsyncClient`，复用现有 `stream` 方法已有的 async 模式。同步 `complete` 保留，供非 async 调用方或迁移期使用。
- **Runtime 异步化**：`AgentRuntimeService.run` 改为 `async def run`，内部所有 LLM 调用改用 `await self.ai.acomplete(...)`（记忆摘要、意图分类、查询改写、风险评估四处）。`LangGraphAgentRuntimeService.run` 用 `await self.graph.ainvoke(...)`。
- **Checkpointer 接入**：`graph.compile(checkpointer=...)`，`thread_id = session.public_id`。存储后端需能跨进程重启持久化（如 SqliteSaver 或 PostgresSaver），MemorySaver 仅用于开发。具体后端在实现时确定，但必须满足崩溃恢复要求。
- **Redis 记忆降级**：`RedisShortTermMemoryStore` 保留为消息历史缓存（供 `MemoryAgent` 加载近期消息）；Agent 状态（intent、risk、retrieved knowledge、response plan）的唯一事实源迁移到 Checkpointer。避免双源真相。
- **interrupt 放置点**：在 risk_guardian 节点，当 `assessment.risk == HIGH` 时调用 `interrupt(...)`。中断发生在 `CounselorAgent` 生成回复之前，因此批准后 resume 会触发 `CounselorAgent` 生成回复。
- **run() 接口变更**：`run()` 的返回需区分"已完成"（含 `response_messages`，可直接流式输出）和"待审核"（被 interrupt，含审核上下文）。`ChatService` 据此分支：已完成 -> 流式输出回复；待审核 -> 发送含危机资源的确认消息并关闭流。
- **待审队列持久化**：新增一个实体记录待审项（关联 session、PsychologicalReport、风险摘要、状态 pending/approved/rejected、时间戳），用于查询和列表。可恢复的图状态本身存在 Checkpointer 里；辅导员批准/驳回后通过 `Command(resume={"approved": bool})` 恢复图执行。
- **审核 API**：新增 admin 鉴权的端点：列出待审消息、批准、驳回。批准 -> 恢复图，`CounselorAgent` 生成回复并投递给学生；驳回 -> 发送安全兜底回复（含危机资源联系方式）。
- **CHAT 路径不变**：`IntentType.CHAT` 直接路由到 `CompanionAgent`，不进入 `RiskGuardianAgent`，不触发 interrupt，保持当前快速路径。
- **Custom runtime 对等**：自研 runtime（LangGraph 不可用时的回退）同样异步化，但没有 Checkpointer / interrupt 能力--HIGH 风险返回待审核状态，由 ChatService 发送固定安全确认并创建人工审核记录。
- **告警集成**：新的待审项通过现有 tool queue / email alert 机制通知辅导员（复用 `alert_email` 配置或新增 `ToolJobKind`），不另造通知系统。
- **GraphState 不拆分**：当前 `GraphState` 是单字段 `{"context": AgentContext}`，本 feature 保持不变。细粒度 State + reducer 拆分是 §A.5，out of scope。
- **LangGraph API 形态**：`Command`、`interrupt`、`Checkpointer` 的 API 形态需对照 LangGraph 0.4.3（`requirements.txt` 已锁定版本）的官方文档核实后再写代码。

## Testing Decisions

- **只测外部行为**：不测 LangGraph 内部机制、节点调用次数、图结构。通过 `run()` 的返回值（intent、risk、response_messages、pending 状态）验证行为对不对，不验证内部怎么实现。
- **主 seam：`AgentRuntimeService.run()` / `LangGraphAgentRuntimeService.run()`**：通过设置 `ai_provider` 为 mock provider（复用现有 `AiClient._mock` 的确定性回复）注入假 LLM，免外部依赖。喂各种输入，观察返回结果的路由 / 风险 / 回复 / interrupt 状态。
- **关键测试用例**：
  - CHAT 输入 -> 已完成结果，不触发 interrupt，`CompanionAgent` 回复。
  - CONSULT 输入 + LOW/MEDIUM 风险 -> 已完成结果，`CounselorAgent` 回复，不 interrupt。
  - RISK 输入 + HIGH 风险 -> 待审核结果，interrupt 被触发。
  - `resume(approved=True)` -> 已完成结果，含 `CounselorAgent` 回复。
  - `resume(approved=False)` -> 已完成结果，含安全兜底回复。
  - async `run()` 与同步逻辑行为一致（parity）。
  - Checkpointer：相同 `thread_id` 跨两次 `run()` 调用状态延续；模拟"崩溃"（新建 runtime 实例）后能从 checkpoint 恢复。
- **集成 seam：`ChatService.prepare()` / `stream_chat()`**：端到端验证 DB 持久化与 SSE 行为。HIGH 风险 -> 验证确认消息含危机资源；非 HIGH -> 验证正常流式。同样用 mock AiClient。
- **Custom runtime**：验证 async parity，并验证 HIGH 风险返回待审核状态（不依赖 interrupt）。
- **Prior art**：`AiClient._mock` 是现有确定性测试模式；`rag_eval/runner.py` 是现有端到端跑系统的模式（用 dataset 驱动、比对预期）。
- **不测**：LangGraph 内部、Chroma 向量检索（hybrid 兜底即可）、SSE 传输层字节细节、辅导员前端 UI。

## Out of Scope

- **A.1 Command 路由**：用 `Command(goto=...)` 替代 `add_conditional_edges` 的内部重构，无用户可见价值。
- **A.5 细粒度 GraphState + reducer**：把 `GraphState` 拆成多字段配 reducer 的内部重构。
- **A.4 中间事件流式**：用 `astream_events` 向前端推送"正在检索知识库""正在评估风险"等中间进度。是异步化的自然延伸，但独立成后续 feature。
- **C RAG 增强**：rerank、真混合检索（向量+BM25 RRF）、多查询、parent-document。有独立评测框架支撑，单独 feature。
- **D 可观测性**：LangSmith / LangFuse trace 接入。
- **E 工程化补齐**：密码哈希换 bcrypt、废弃 API 迁移（`on_event`/`utcnow`）、其他模块单测。独立 feature，不阻塞本 PRD。
- **F 可靠性与扩展**：tool queue 换 broker、chat 接口限流。
- **辅导员审核前端 UI**（`app/static/`）：本 PRD 只交付后端 API。
- **MEDIUM 风险人审**：只有 HIGH 触发审核；MEDIUM 及以下自动处理。
- **更换 SSE 传输协议**：保持现有 SSE，不改传输层。
- **辅导员通知机制的重构**：只复用现有 alert/tool queue，不另造通知系统。

## Further Notes

- 本 PRD 对应研究报告 §A.2 + §A.3 + §B，记录的是 2026-07-15 实施前的代码状态（当时 `langgraph_runtime.py` 尚无 checkpointer、`graph.invoke` 会阻塞、`chat.py:prepare` 为同步流程）。
- LangGraph 版本 0.4.3 已锁定在 `requirements.txt`。`Command` / `interrupt` / `Checkpointer` 的 API 形态基于 0.4.x 的认知，实现前必须对照官方文档核实。
- 自研 runtime 回退路径无 Checkpointer，无法支持 interrupt--回退模式下 HIGH 风险返回待审核状态，由统一的 ChatService 确认和人工审核队列承接。
- 这是研究报告中 ROI 最高的升级方向，也是核心"面试爆点"（human-in-the-loop 安全边界）。
- 现有 `rag_eval` 评测框架（5 个指标）不受本 feature 影响--本 PRD 不改 RAG 检索逻辑。
- 当前项目已有 `CONTEXT.md` glossary 和相关 ADR；本历史记录中的建议已由 ADR-0003、ADR-0005 和 ADR-0010 覆盖。
- 工程化补齐（§E）虽 out of scope，但建议在动手本 feature 前先补 `assessment.py` 三层逻辑的单测，作为人审 interrupt 测试的基线（`RiskGuardianAgent` 的 HIGH 判定是人审的触发前提）。
