Status: ready-for-agent

# 02: Async end-to-end（prepare -> runtime -> LLM 全链路异步）

## Parent

../spec.md (langgraph-deep-upgrade)

## What to build

把从 `ChatService.prepare` 到 agent runtime 到语言模型调用的全链路异步化，消除 prepare 阶段同步阻塞事件循环的问题。AiClient 新增异步 completion 方法；agent runtime 的 `run` 改为 async，内部模型调用用异步接口，LangGraph 用 `ainvoke`；`ChatService.prepare` 改 async，`stream_chat` 用 await。日常对话和心理支持路径都异步化。同步 `complete` 保留供迁移期使用。Custom runtime（LangGraph 不可用回退）同步异步化，并对 HIGH 风险返回待审核状态。

## Acceptance criteria

- [ ] AiClient 异步 completion 行为与同步一致（ollama / openai / mock 三 provider）
- [ ] agent runtime `run` 改 async，CHAT 输入产出的结果与异步前一致
- [ ] LangGraph runtime 用 `ainvoke`，行为与异步前一致
- [ ] `ChatService.prepare` 改 async，不再阻塞事件循环
- [ ] 异步路径测试用 mock AiClient，验证 CHAT 和 support 路径结果 parity
- [ ] Custom runtime 同样异步化

## Blocked by

None - can start immediately
