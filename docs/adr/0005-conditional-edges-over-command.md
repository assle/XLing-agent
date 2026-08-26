# 5. 驳回路径用条件边而非 Command 路由

日期：2026-07-15

## 状态

Accepted

## 背景

人工审核恢复（[issue 05](../../.scratch/langgraph-deep-upgrade/issues/05-counselor-resume-approve-reject.md)）的驳回路径需要跳过 CounselorAgent 直接到 END--驳回时发固定兜底回复，不需要 AI 生成。

LangGraph 0.4 有两种路由方式：
- **`Command(goto=END, update={...})`**：节点直接 return Command，同时做路由 + 状态更新，更内聚
- **`add_conditional_edges`**：独立的 router 函数返回字符串，映射到目标节点

PRD（`langgraph-deep-upgrade/spec.md`）把 §A.1（Command 路由）列为 **out of scope**："用 `Command(goto=...)` 替代 `add_conditional_edges` 的内部重构，无用户可见价值"。

## 决策

用 `add_conditional_edges`（条件边），不用 Command。

- `risk_guardian_gate` 节点在驳回时设 `context.response_planned = True` 作为信号
- 条件边 `_route_after_gate`：`response_planned` 为 True -> END（驳回），False -> counselor（批准/正常）
- 批准路径：gate 不设 flag -> 走 counselor -> AI 生成回复
- 驳回路径：gate 设 flag -> 走 END -> 跳过 counselor -> 兜底回复

## 后果

正面：
- 在 PRD scope 内（不引入 out-of-scope 的 A.1 Command 路由）
- 复用项目已有的条件边模式（supervisor 路由已经是 `add_conditional_edges`）
- 保持图结构一致

负面：
- 多一个 `response_planned` flag 和 `_route_after_gate` 路由函数
- 不如 `Command(goto=END)` 内聚（Command 能在一个 return 里同时路由 + 更新状态）
- 未来如果做 §A.1（Command 路由），这个条件边可以重构掉

## 相关

- Issue 05（counselor resume）
- PRD §A.1（Command 路由，out of scope）
- [ADR-0003](0003-interrupt-node-splitting.md)（gate 节点的另一个职责：interrupt）
