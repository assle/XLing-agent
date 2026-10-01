# 5. 使用条件边表达审核与支持路径

日期：2026-07-15

## 状态

Accepted

## 背景

人工审核拒绝必须结束自动支持回复生成，批准才继续。四维追问完成或已准备下一问时也可以结束本轮，但两种业务结果需要分别表达。

## 决策

沿用 `add_conditional_edges` 表达路由，节点返回业务字段更新。`Command(resume=...)` 只用于把人工决定送回暂停点。

- 审核关口返回 `review_decision`：拒绝进入 `END`，批准进入 `counselor`；低、中风险没有审核决定，继续 `knowledge`。
- 四维追问节点返回独立的 `cbt_event` 和可选 `action_plan_event`。已有追问或计划回复时结束本轮；行动计划创建失败、没有可用事件时继续 `counselor`。
- 条件边读取业务结果；审核决定与追问事件各有明确含义，不共用回复进度标志。

## 后果

审核和支持分支保持一致的图连接方式。业务事件直接用于网页呈现，路由不依赖旧调度循环的资格标志；诊断同时记录所选分支和对应判断值。

## 相关

- [ADR-0003](0003-interrupt-node-splitting.md)
- [ADR-0010](0010-safety-assessment-before-knowledge.md)
- [ADR-0012](0012-langgraph-runtime-and-local-diagnostics.md)
