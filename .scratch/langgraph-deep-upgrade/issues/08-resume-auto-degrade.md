Status: ready-for-agent

# 08: Resume 检测 checkpoint 丢失时自动降级

## Parent

../spec.md (langgraph-deep-upgrade)

## What to build

当辅导员对一个 pending 待审消息执行 approve/reject 时，如果该消息的 LangGraph checkpoint 已丢失（如服务重启后 MemorySaver 是内存的、checkpoint 没持久化），resume 不应崩溃或让审核卡死，而是**自动降级**：检测到无 checkpoint 就把该 ReviewRequest 标记为 `escalated` 并发送固定兜底回复给学生，和超时升级（issue 07）走同一条兜底路径。

检测条件：`get_state(thread_id).values` 为空（没有 context）即代表无 checkpoint。有 checkpoint 时 `.values` 含 `{"context": AgentContext}`，重启后 MemorySaver 丢了就是空 dict。

这个 issue 来自一次 grilling session 的决策：项目是单进程学习项目，MemorySaver 共享 singleton 够用（interrupt/resume 跨请求工作），但重启会丢 checkpoint。与其为重启恢复去解决 AsyncSqliteSaver 的异步初始化复杂度，不如在 resume 失败时安全降级。

## Acceptance criteria

- [x] resume 检测到无 checkpoint（`get_state(thread_id).values` 为空）时不抛异常，走降级路径
- [x] 降级路径：ReviewRequest 标记为 `escalated`，学生收到固定兜底回复（与 issue 07 超时升级相同的 fallback）
- [x] 降级路径对 approve 和 reject 都生效（无论辅导员点什么，无 checkpoint 都走兜底）
- [x] 正常 resume（checkpoint 存在）行为不变：approve 生成 AI 回复，reject 发兜底
- [x] 测试模拟"重启"（新 MemorySaver 实例 / 清空 checkpoint）+ resume -> 验证自动降级
- [x] 测试验证正常 resume（checkpoint 存在）仍工作

## Blocked by

None - can start immediately

## Comments

**2026-07-15 已实现。**

- `AgentRunResult.degraded: bool` 新字段标记降级
- `resume()` 开头检查 `get_state(thread_id).values` 为空 -> 返回 `degraded=True` + `fallback_response`，不调 ainvoke
- `ReviewService.mark_escalated(review_id)` 新方法
- approve/reject API 端点检查 `result.degraded`：是则 mark_escalated + 发兜底，否则走正常 approve/reject
- 4 个新测试（3 resume 降级 + 1 mark_escalated），65 tests total 全通过

## Comments

**2026-07-15 grilling 决策记录。** 

背景：issue 03 用 MemorySaver（内存）做 checkpointer。单进程下 interrupt/resume 跨请求工作（共享 singleton），但重启后 checkpoint 丢失。这导致 DB 里 ReviewRequest 仍显示 pending（DB 持久化），但 resume 拿 thread_id 去 MemorySaver 找不到 checkpoint，图空跑，行为未定义。

grilling 达成的决策：
- 部署模型：单进程（学习/面试项目，生产部署 out of scope）
- async vs sync：保持 async（面试卖点：解决事件循环阻塞 > checkpointer 持久化）
- checkpointer：MemorySaver 共享 singleton（单进程够用）
- 重启丢 checkpoint：**option b 自动降级**（本 issue）

未选的选项：
- option a（接受、文档记录）-- pending 审核会卡死，不够健壮
- option c（AsyncSqliteSaver 懒初始化）-- 设计改动大，为单进程学习项目的边缘场景不值得
