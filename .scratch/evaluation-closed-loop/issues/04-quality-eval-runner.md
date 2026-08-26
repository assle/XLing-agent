Status: ready-for-agent

## Parent

`.scratch/evaluation-closed-loop/spec.md`

## What to build

新建对话质量评测 runner 和 judge prompt。runner 读取场景集 JSON（每条含 id、text、category、notes），用 CounselorAgent 的 system prompt + 真实 LLM（temperature=0.3）生成回复，再用不同的 judge 模型（temperature=0）按 4 个维度（共情性/安全性/行动落地/边界守持）打 1-5 分。每个维度有明确的 1/3/5 分锚点描述。judge 输出 JSON（4 个分数 + 各维度理由）。

runner 对同一批评测跑 3 次 judge，计算每个维度的均值和标准差。标准差 > 0.5 的维度在报告中标注不稳定。输出完整报告 JSON（逐条回复 + 4 维度评分 × 3 次 + 均值 + 标准差）和汇总摘要 JSON（只含维度均分 + 标准差）。

judge prompt 存入 `app/quality_eval/judge_prompt.py`，包含维度定义、分数锚点、输出 JSON 格式约束。

runner 支持两种模式：真实 LLM 模式（生成和 judge 用不同 provider/model，通过配置项独立设置，都走 OpenAI 兼容接口）和 mock 模式（用 FakeAiClient 返回 canned 回复和 canned judge 评分，用于 CI 回归）。运行方式为 `python -m app.quality_eval.runner`，可选 `--provider mock` 参数。

新增配置项到 `app/core/config.py`：场景集路径、报告输出路径、摘要输出路径、生成 provider、judge provider、judge model、judge base_url、judge api_key、judge 运行次数，风格与现有配置项一致。

## Acceptance criteria

- [ ] `app/quality_eval/runner.py` 的 `evaluate()` 能读取场景集、生成回复、调 judge 打分、输出报告
- [ ] judge prompt 含 4 个维度的 1/3/5 分锚点描述
- [ ] judge 输出 JSON 能正确解析（4 个分数 + 理由）
- [ ] 3 次 judge 跑完后，均值和标准差计算正确（用已知输入验证）
- [ ] 标准差 > 0.5 的维度在报告中标注不稳定
- [ ] 完整报告 JSON 含逐条回复 + 4 维度评分 × 3 次 + 均值 + 标准差
- [ ] 汇总摘要 JSON 只含维度均分 + 标准差
- [ ] mock 模式能跑通完整流程并输出结构正确的报告
- [ ] `--provider mock` 参数能切换到 mock 模式
- [ ] 生成回复用 temperature=0.3，judge 用 temperature=0
- [ ] 新增配置项加入 `Settings` 类，judge 配置独立于生成配置
- [ ] 测试用 FakeAiClient 返回 canned 回复和 canned judge 评分验证解析和计算正确性
- [ ] 测试不断言 runner 内部实现细节，只验证外部行为

## Blocked by

None - can start immediately
