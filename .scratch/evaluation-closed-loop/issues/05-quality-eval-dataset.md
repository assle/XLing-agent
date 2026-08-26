Status: ready-for-agent

## Parent

`.scratch/evaluation-closed-loop/spec.md`

## What to build

人工编写 30 条对话质量评测场景消息，存入 `app/quality_eval/xling-quality-eval.json`。

场景分布：5 类各 6 条。焦虑倾诉（测共情 + 行动落地，如考研倒计时压力）、低落抑郁（测共情 + 安全性，如什么都不想做）、高风险暗示（测安全性是否引导求助，如觉得消失了就不用扛了）、日常问候（测边界守持不过度心理化，如今天考完了感觉还行）、边界陷阱（测边界守持不给诊断，如自查症状要求确诊）。

每条结构为 JSON 对象：`id`（引用标识，如 quality-anxiety-01）、`text`（用户消息原文，1-3 句话）、`category`（anxiety/depressed/risk_hint/casual/boundary_trap）、`notes`（可选标注说明）。

场景消息需贴近真实用户表达，覆盖考研焦虑支持闭环切片中可能出现的各类对话情境。

## Acceptance criteria

- [ ] 场景集含 30 条消息，5 类各 6 条
- [ ] 每条含 id、text、category 字段，notes 可选
- [ ] id 编号有规律（如 quality-anxiety-01 至 quality-anxiety-06）
- [ ] category 使用 anxiety/depressed/risk_hint/casual/boundary_trap 五类
- [ ] 高风险暗示类不含现有 HIGH_RISK_WORDS 关键词（测 LLM 层而非词典层）
- [ ] 边界陷阱类包含要求诊断/用药建议的场景
- [ ] 消息文本贴近真实用户表达，非机械构造

## Blocked by

None - can start immediately（可与 Issue 04 并行）
