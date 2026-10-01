# 3. 将风险评估与人工审核关口分成两个节点

日期：2026-07-15

## 状态

Accepted

## 背景

LangGraph 的 `interrupt()` 会暂停当前节点，节点尚未返回的字段更新不会成为已保存状态。若在同一节点先评估再暂停，恢复时可能缺少本次安全风险评估结果。

## 决策

- `risk_guardian` 完成安全风险评估与风险轨迹判断，返回 `assessment`、`risk_level` 等原生图字段更新，让 LangGraph 正常保存。
- 独立的 `risk_guardian_gate` 读取已保存的风险等级，高风险时调用 `interrupt()` 等待人工决定。
- 恢复会重新进入审核关口；`interrupt()` 返回人工决定，节点随后更新独立的 `review_decision` 业务字段。
- 低、中风险在关口后进入知识检索；高风险批准后进入支持回复规划，拒绝结束图执行并使用固定安全回复。

图连接为 `risk_guardian -> risk_guardian_gate`。节点读取原生状态并返回字段更新，不原地修改共享上下文；本轮状态协议见 ADR-0012。

## 后果

暂停前已有完整可恢复的评估结果。重新进入审核关口不会重复风险评估或风险轨迹写入；正常暂停在诊断中作为中断记录。

## 相关

- [ADR-0002](0002-checkpointer-memorysaver.md)
- [ADR-0005](0005-conditional-edges-over-command.md)
- [ADR-0010](0010-safety-assessment-before-knowledge.md)
- [ADR-0012](0012-langgraph-runtime-and-local-diagnostics.md)
