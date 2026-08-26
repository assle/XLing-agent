Status: ready-for-agent

# 02: Structured risk output + tenacity 有界重试

## Parent

../spec.md (exam-anxiety-closed-loop)

## What to build

把当前风险分析的手工 JSON 文本约定替换为 schema 约束的 Structured Output，并建立后续 CBT 状态与行动计划可复用的结构化生成边界。模型或网络出现明确可重试的暂时错误、返回不符合 schema 的内容时，由 tenacity 做有上限且带退避的重试；不可重试错误或重试耗尽时进入保守、安全、可观察的 fallback，绝不把半解析结果写成有效风险状态。

这一 slice 贯穿 AI provider、风险评估 service、agent runtime 和学生对话：正常结构化结果维持既有分流，高风险关键词仍能在不依赖模型的情况下优先进入安全路径，模型故障时用户得到安全响应而不是 500 或未经验证的业务状态。

## Acceptance criteria

- [x] 风险评估结果由声明式 schema 校验 emotion、emotion score、risk、confidence 与 summary
- [x] 支持的真实 provider 使用其可用的结构化输出能力或等价的 schema 约束协议，mock provider 提供确定性结构化结果
- [x] schema 不合法、暂时网络错误和可重试 provider 错误会按配置的有限次数与退避策略重试
- [x] 永久错误不做无意义重试；重试耗尽后返回保守安全 fallback，不产生未验证的评估记录
- [x] 明确 HIGH 风险关键词无需等待结构化模型成功即可进入既有安全/人审路径
- [x] 正常 CHAT、普通焦虑和 HIGH 风险三条用户路径的既有外部行为不回退
- [x] 日志可区分首次成功、重试后成功和重试耗尽，但不记录原始敏感对话正文
- [x] Security seam 用成功、格式错误、暂时错误、永久错误序列验证最终行为，不断言 tenacity 内部调用细节

## Blocked by

None - can start immediately


## Comments

### 2026-07-22 实现完成

- `app/services/assessment.py`：完全重写。新增 `RiskAssessmentSchema`（Pydantic schema 约束 emotion/emotionScore/risk/confidence/summary），`TransientAssessmentError` / `PermanentAssessmentError` 区分可重试和不可重试错误，tenacity 有界重试（指数退避 0.5-4s，默认 3 次），`safe_fallback_assessment` 保守 MEDIUM 风险兜底
- HIGH 风险关键词仍绕过结构化模型直接进入安全路径
- 永久错误（4xx）不重试，暂时错误（timeout/network/schema 不合法/JSON 解析失败）重试
- `tests/test_assessment.py`：27 个测试覆盖 schema 验证、重试行为、永久/暂时/格式错误 fallback、异步变体
