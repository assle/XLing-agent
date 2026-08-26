Status: ready-for-agent

## Parent

`.scratch/evaluation-closed-loop/spec.md`

## What to build

用真实 LLM 跑对话质量评测，调试 judge prompt 的 rubric 锚点描述，直到各维度标准差 < 0.5。

Issue 04 交付的 judge prompt 是初版，需要用真实 LLM 跑完整 30 条场景评测后，检查打分分布和稳定性。如果某维度标准差 > 0.5，说明 judge 对该维度的理解不稳定，需要优化该维度的锚点描述（比如把"3 分：有建议但太笼统"改成更具体的"3 分：有建议但缺少具体时间、步骤或场景"）。

调试过程中可能需要多轮迭代：跑评测 -> 检查标准差 -> 修改 prompt -> 重跑。修改的是 `app/quality_eval/judge_prompt.py` 中的维度定义和锚点描述。

最终交付：调试完成的 judge prompt + 一份真实评测报告（完整报告 JSON + 汇总摘要 JSON），报告中的各维度标准差均 < 0.5。

## Acceptance criteria

- [ ] 用真实 LLM 跑完整 30 条场景评测（生成回复 + 3 次 judge 打分）
- [ ] 4 个维度的标准差均 < 0.5
- [ ] judge prompt 的锚点描述经过至少一轮调试优化
- [ ] 最终评测报告输出到 target/ 目录
- [ ] 报告中的逐条明细可用于面试 bad case 分析

## Blocked by

- `.scratch/evaluation-closed-loop/issues/04-quality-eval-runner.md`
- `.scratch/evaluation-closed-loop/issues/05-quality-eval-dataset.md`
