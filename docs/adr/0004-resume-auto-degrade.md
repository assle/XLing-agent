# 4. Resume 检测 Checkpoint 丢失时自动降级

日期：2026-07-15

## 状态

Accepted

## 背景

[ADR-0002](0002-checkpointer-memorysaver.md) 选择 MemorySaver（内存）做 checkpointer。MemorySaver 不持久化跨重启--服务重启后 checkpoint 丢失。

但 ReviewRequest 存在 DB 里（持久化），重启后 DB 仍显示 `status=pending`。断裂点：
- DB 说"有 pending 审核"（持久化，重启不丢）
- MemorySaver 丢了 checkpoint（内存，重启清空）
- 辅导员点批准/驳回，`resume(thread_id)` 去 MemorySaver 找不到 checkpoint，图从入口节点空跑，`KeyError: 'context'` 崩溃

pending 审核卡死，辅导员无法处理。

## 决策

在 `resume()` 开头检查 checkpoint 是否存在：`get_state(thread_id).values` 为空即代表无 checkpoint（有 checkpoint 时 `.values` 含 `{"context": AgentContext}`）。

无 checkpoint 时**自动降级**，不调 ainvoke：
- 返回 `AgentRunResult(degraded=True, fallback_response=固定兜底回复)`
- 调用方（API 端点）检测 `degraded` -> 把 ReviewRequest 标记为 `escalated` + 发兜底回复给学生
- 对 approve 和 reject 都生效（无论辅导员点什么，无 checkpoint 都走兜底）

## 后果

正面：
- pending 审核不卡死，学生能收到兜底回复
- 不抛未处理异常，优雅降级
- 面试可讲健壮性设计："我考虑了服务重启时的降级策略--检测 checkpoint 丢失就安全降级到兜底，而不是让审核卡死"

负面：
- 重启后的 pending 审核拿不到原 context（intent/risk/knowledge），只能发兜底回复，无法走正常的 AI 回复或驳回路径
- 降级结果的 `intent` 等字段是占位符（原值随 checkpoint 丢了），但调用方只用 `degraded` + `fallback_response`，无影响

## 考虑过的替代方案

- **option a（接受、文档记录）**：pending 审核卡死，辅导员无法处理。不够健壮。
- **option c（AsyncSqliteSaver 懒初始化）**：真正的持久化，但需把 checkpointer 创建从 `__init__` 移到 async `run()` 首次调用，设计改动大。单进程学习项目的边缘场景不值得。
- **option b（本决策）**：降级而非持久化。简单、健壮、够用。

## 相关

- [ADR-0002](0002-checkpointer-memorysaver.md)（MemorySaver 不持久化是本 ADR 的起因）
- Issue 08（resume auto-degrade）
- Grilling session 决策：单进程 + 保持 async + option b 降级
