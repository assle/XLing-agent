Status: ready-for-agent

# 03: Checkpointer 会话状态持久化 + Redis 降级

## Parent

../spec.md (langgraph-deep-upgrade)

## What to build

给 LangGraph graph 接入 Checkpointer，以 session 的 `public_id` 作为 `thread_id`，每轮对话自动快照 Agent 状态。Redis 短期记忆降级为消息缓存（供 MemoryAgent 加载近期消息），Agent 状态（intent / risk / retrieved knowledge / response plan）的唯一事实源迁到 Checkpointer，消除双源真相。存储后端用 SqliteSaver（文件持久化，跨重启恢复）。MemorySaver 仅开发用。

> 约束：项目用 MySQL，LangGraph 无 MySQLSaver。SqliteSaver 是最少基建的持久化选择（引入一个 SQLite 文件，无需新服务）。如实现时发现有更强理由用 PostgresSaver，回 spec 确认。

## Acceptance criteria

- [ ] graph compile 时传入 checkpointer，thread_id = session.public_id
- [ ] 相同 session 两次 run 之间，Agent 状态通过 checkpoint 延续
- [ ] 模拟崩溃（新建 runtime 实例）后，同 thread_id 能恢复上一轮状态
- [ ] Redis 短期记忆仍提供消息历史给 MemoryAgent，但不再承载 Agent 状态
- [ ] Checkpointer 存储跨进程重启持久化
- [ ] 测试用 mock AiClient

## Blocked by

None - can start immediately
