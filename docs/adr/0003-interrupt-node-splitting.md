# 3. Interrupt 拆成 risk_guardian + risk_guardian_gate 两个节点

日期：2026-07-15

## 状态

Accepted

## 背景

人工审核中断（[issue 04](../../.scratch/langgraph-deep-upgrade/issues/04-interrupt-trigger-acknowledgment.md)）需要 RiskGuardianAgent 评估为 HIGH 时调用 `interrupt()` 暂停图执行，停在 CounselorAgent 生成回复之前。

最初的设计是在单个 `risk_guardian` 节点里先调 `risk_guardian_agent`（执行评估、设置 `context.risk_level = HIGH`、`context.assessment`），再调 `interrupt()`。

**问题**：测试发现 interrupt 后 checkpoint 里的 context **丢失了评估变更**--`risk_level` 还是 LOW、`assessment` 是 None。

根因：LangGraph 在节点 `interrupt()` 时保存的是**节点开始前**的状态（上一节点完成后的 checkpoint）。节点内的 in-place mutation（`risk_guardian_agent` 设置的 risk/assessment）没有被保存，因为节点没有正常 return（被 interrupt 打断），return 值不会被应用。

## 决策

把 risk_guardian 拆成两个节点：

- **`risk_guardian`**：执行 `risk_guardian_agent`（评估、设置 risk/assessment），正常 return。其 mutation 被 checkpoint 保存。
- **`risk_guardian_gate`**：检查 `context.risk_level == HIGH`，是则 `interrupt()`。此时 risk_guardian 的评估变更已经在上一个 checkpoint 里了。

图流程：`risk_guardian -> risk_guardian_gate`；非高风险在 gate 后进入知识检索，高风险批准后进入 CounselorAgent。

gate 节点 interrupt 时，保存的状态来自 risk_guardian 完成后（含评估变更），resume 时能拿到完整的 context。

## 后果

正面：
- 评估变更（risk=HIGH, assessment, emotion_score）在中断前被 checkpoint 保存
- resume 时 CounselorAgent 能基于完整 context 生成回复
- 这是一个非显然的 LangGraph 行为洞察，面试可讲："我发现节点中断时保存的是节点开始前的状态，in-place mutation 会丢，所以拆成两个节点"

负面：
- 图多一个节点，稍复杂
- gate 节点逻辑简单（检查 + interrupt），看似"浪费"一个节点

## 相关

- Issue 04（interrupt 触发）
- Issue 05（resume 依赖此设计拿到完整 context）
- LangGraph interrupt 语义：节点 interrupt 时保存节点开始前状态，节点 return 值才会更新 state
