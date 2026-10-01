# 2. 使用异步 SQLite 持久化人工审核检查点

日期：2026-08-30

## 状态

Accepted

## 背景

人工审核中断与恢复需要保存 LangGraph 图状态。项目最初只面向单进程演示，因此选择 MemorySaver；服务重启后由 ADR-0004 安全降级。面试项目增强要求增加一个可复现的“中断后完全重启仍能恢复”演示，同时保持 ADR-0001 的异步运行方式。

兼容性验证结果：

- 官方 Redis 检查点 `langgraph-checkpoint-redis 0.1.3` 兼容当前 `langgraph 0.4.3`，但要求 Redis Search/JSON 模块；项目和现有 Docker 使用普通 Redis，初始化时 `FT.INFO` 失败。
- 官方 `langgraph-checkpoint-sqlite 2.0.11` 兼容当前 LangGraph，并提供 `AsyncSqliteSaver`。
- `aiosqlite 0.22` 移除了该版本检查点使用的连接存活接口，因此固定使用兼容的 `aiosqlite 0.20.0`。

## 决策

默认使用 `AsyncSqliteSaver`：

- 检查点文件为 `data/langgraph-checkpoints.db`；
- 在首次异步运行或恢复时懒初始化连接；
- 每次网页请求完成后关闭运行时持有的 SQLite 连接；
- 原生 `GraphState` 只保存编号、枚举值字符串、布尔值、数值、列表和字典；每次新消息完整初始化本轮字段；
- 不保存 SQLAlchemy 模型或数据库会话；
- `JsonPlusSerializer` 不启用 pickle 回退；
- 使用独立活动表记录线程更新时间，默认保留 30 天；
- 用户删除账号时，在业务数据提交后删除对应检查点；失败时把清理任务持久化，并在后续启动时重试；
- MemorySaver 只作为测试和显式本地适配器保留。

## 后果

正面：

- 人工审核可以跨运行时实例和服务重启恢复；
- 检查点内容不依赖任意 Python 对象反序列化；
- 不需要更改当前普通 Redis 或 Docker 基础设施；
- 过期检查点可自动清理。

负面：

- SQLite 适合单机面试项目，不宣称支持多主机分布式写入；
- 运行时需要管理异步连接生命周期；
- 原生图状态不迁移旧检查点；本地切换按 ADR-0012 清理旧会话及其关联数据。新流程检查点不可用时安全降级。

## 仍然保留的安全降级

检查点不存在、过期、损坏或持久化存储不可用时，继续执行 ADR-0004 的固定安全回复和人工审核升级，不允许审核流程卡死。

## 相关

- [ADR-0001](0001-async-runtime.md)
- [ADR-0003](0003-interrupt-node-splitting.md)
- [ADR-0004](0004-resume-auto-degrade.md)
- [ADR-0012](0012-langgraph-runtime-and-local-diagnostics.md)
- GitHub Issue #13
