# 2. Checkpointer 用 MemorySaver 而非 SqliteSaver

日期：2026-07-15

## 状态

Accepted

## 背景

interrupt/resume（人工审核）需要 checkpointer 持久化中断时的图状态（AgentContext）。LangGraph 0.4.3 提供三个选项：

- **MemorySaver**（InMemorySaver）：内存存储，原生支持 sync + async
- **SqliteSaver**：SQLite 文件持久化，**只支持 sync**（`invoke`），不支持 `ainvoke`
- **AsyncSqliteSaver**：支持 async，但需要 `aiosqlite` 异步连接，其初始化与 sync `__init__` 冲突（`__init__` 是同步的，但 AsyncSqliteSaver 需要在 async context 里建立连接）

项目约束（grilling 确认）：
- **单进程**学习/面试项目，生产部署 out of scope
- runtime 已异步化（[ADR-0001](0001-async-runtime.md)），checkpointer 必须支持 `ainvoke`
- interrupt/resume 的真实场景是"学生发消息 -> 辅导员在同一个运行中的服务审核"，不是"服务崩了重启后恢复"

## 决策

用 **MemorySaver** 作为 checkpointer，做模块级共享 singleton。

- `graph.compile(checkpointer=MemorySaver(...))`
- `thread_id = session.public_id`
- 共享 singleton：所有 runtime 实例用同一个 MemorySaver，让 API 端点的新 runtime 实例能访问原请求的 checkpoint（跨请求 resume）
- `JsonPlusSerializer(pickle_fallback=True)` 处理 AgentContext 里的 SQLAlchemy 模型等不可 msgpack 序列化的对象

## 后果

正面：
- 原生支持 async（`ainvoke`），不与 [ADR-0001](0001-async-runtime.md) 冲突
- 共享 singleton 让 interrupt/resume 跨请求工作（已测试验证）
- 无外部依赖（不需要 aiosqlite/Postgres）

负面：
- **不持久化跨重启**：服务重启后 checkpoint 丢失。见 [ADR-0004](0004-resume-auto-degrade.md) 的降级处理。
- **单进程限制**：多进程部署时每个 worker 有独立内存，共享 singleton 失效。多进程需换 AsyncSqliteSaver。
- **内存不清理**：旧 checkpoint 不自动清除，长期运行内存增长。学习项目可接受。

## 考虑过的替代方案

- **SqliteSaver**：持久化但不支持 async，与 [ADR-0001](0001-async-runtime.md) 冲突。要回退异步化才可用，代价过大。
- **AsyncSqliteSaver**：支持 async + 持久化，但需把 checkpointer 创建从 `__init__` 移到 async `run()` 首次调用（懒初始化），设计改动大。单进程学习项目不值得。
- **PostgresSaver**：需要引入 Postgres（项目用 MySQL），基建成本高。

## 相关

- 研究报告 §A.2（Checkpointer）
- Issue 03（checkpointer）
- Grilling session 决策：单进程 + 保持 async
- 衍生 [ADR-0004](0004-resume-auto-degrade.md)（重启丢 checkpoint 的降级）
