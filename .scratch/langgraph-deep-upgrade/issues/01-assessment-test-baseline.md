Status: ready-for-agent

# 01: Assessment 三层风险评估单测基线

## Parent

../spec.md (langgraph-deep-upgrade)

## What to build

为 `PsychologicalAssessmentService.assess` 的三层风险评估逻辑补参数化单测，作为后续人审 interrupt 测试的基线。三层逻辑：关键词直判 HIGH（不经 LLM）、LLM 返回 JSON 评估、`risk_from_score` 取最高复核。用 mock AiClient（`ai_provider=mock`）免外部 LLM 依赖。同时覆盖 `RiskGuardianAgent` 在 `intent==RISK` 时强制覆写 risk=HIGH 的 runtime 层逻辑--这是人审 interrupt 的触发前提。

## Acceptance criteria

- [ ] `has_high_risk_signal` 命中时直接返回 HIGH，不调 LLM
- [ ] LLM 返回 LOW 但 `emotion_score >= 4` 时，`risk_from_score` 取最高覆写为 HIGH
- [ ] `emotion == HIGH_RISK` 时强制 risk = HIGH
- [ ] LLM 调用异常时回退 heuristic
- [ ] `intent == RISK` 且 assessment 非 HIGH 时，保留安全风险评估结果，不由消息类型强制覆写
- [ ] 全部测试用 mock AiClient，无外部 LLM/DB 依赖
- [ ] 测试只验证外部行为（输入 -> 输出），不测内部实现细节

## Blocked by

None - can start immediately
