Status: ready-for-agent

## Parent

`.scratch/evaluation-closed-loop/spec.md`

## What to build

人工编写 50 条风险识别评测标注消息，存入 `app/risk_eval/xling-risk-eval.json`。

标注集分布：HIGH 18 条（含直接表达 6 条、委婉暗示 6 条、计划/意图 6 条）、MEDIUM 16 条（持续低落、抑郁情绪、中度困扰）、LOW 16 条（轻度焦虑、日常倾诉、正常情绪）。其中约 8 条为边界陷阱：3 条正常情绪含高风险词（测误报）、3 条委婉自杀意念不含关键词（测漏报）、2 条反讽/引用（测误报）。

每条结构为 JSON 对象：`id`（引用标识，如 risk-high-01）、`text`（用户消息原文，1-3 句话）、`expected_risk`（LOW/MEDIUM/HIGH）、`category`（direct/euphemism/plan/normal_vent/mild_anxiety/trap）、`notes`（可选标注说明，如"含'不想活'但语境是反讽"）。

标注消息需贴近真实用户表达，避免过于直白。HIGH 类的委婉暗示和边界陷阱类尤其需要真实感和模糊性，以暴露词典层和 LLM 层各自的盲区。

## Acceptance criteria

- [ ] 标注集含 50 条消息，分布为 HIGH 18 / MEDIUM 16 / LOW 16
- [ ] 含约 8 条边界陷阱（误报 3 + 漏报 3 + 反讽 2）
- [ ] 每条含 id、text、expected_risk、category 字段，notes 可选
- [ ] id 编号有规律（如 risk-high-01 至 risk-high-18、risk-mid-01 至 risk-mid-16、risk-low-01 至 risk-low-16）
- [ ] category 使用 direct/euphemism/plan/normal_vent/mild_anxiety/trap 六类
- [ ] HIGH 类委婉暗示条目不含现有 HIGH_RISK_WORDS 关键词
- [ ] 边界陷阱的误报条目含 HIGH_RISK_WORDS 但 expected_risk 为 LOW 或 MEDIUM
- [ ] 消息文本贴近真实用户表达，非机械构造

## Blocked by

None - can start immediately（可与 Issue 02 并行）
