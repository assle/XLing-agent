Status: ready-for-agent

## Parent

`.scratch/evaluation-closed-loop/spec.md`

## What to build

新建风险识别评测 runner。读取标注集 JSON（每条含 id、text、expected_risk、category、notes），初始化 AiClient 和 PsychologicalAssessmentService，对每条标注异步调用 `aassess()` 收集预测结果，计算 per-class precision/recall/F1（LOW/MEDIUM/HIGH 各一组）、macro-F1、3x3 confusion matrix，输出完整报告 JSON（逐条明细 + 指标 + confusion matrix）和汇总摘要 JSON（只含指标数字）。

runner 支持两种模式：真实 LLM 模式（通过 `risk_eval_ai_provider` 配置 provider，复用系统现有 OPENAI_API_KEY / OPENAI_BASE_URL / OLLAMA_BASE_URL 等配置）和 mock 模式（走 AiClient._mock 的风险评估分支，用于 CI 回归）。运行方式为 `python -m app.risk_eval.runner`，可选 `--provider mock` 参数。

runner 自带少量测试用标注（5-10 条，覆盖三类），用于 mock 模式下验证指标计算逻辑。完整 50 条标注集由 Issue 03 单独交付。

新增配置项到 `app/core/config.py`：标注集路径、报告输出路径、摘要输出路径、ai provider，风格与现有 `rag_eval_*` 配置项一致。

## Acceptance criteria

- [ ] `app/risk_eval/runner.py` 的 `evaluate()` 能读取标注集、调 `aassess()`、输出报告
- [ ] per-class precision/recall/F1 计算正确（用已知输入验证）
- [ ] macro-F1 计算正确
- [ ] 3x3 confusion matrix 计算正确（行=期望、列=预测）
- [ ] 完整报告 JSON 含逐条明细（每条的预测风险、期望风险、是否命中、category）
- [ ] 汇总摘要 JSON 只含指标数字
- [ ] mock 模式能跑通完整流程并输出结构正确的报告
- [ ] `--provider mock` 参数能切换到 mock 模式
- [ ] 新增配置项加入 `Settings` 类，有合理默认值，通过 `.env` 可覆盖
- [ ] 测试用 FakeAiClient + 少量已知标注验证指标计算正确性
- [ ] 测试不断言 runner 内部实现细节，只验证外部行为

## Blocked by

None - can start immediately
