Status: ready-for-agent

# PRD: 效果评估闭环

> 本 PRD 固化 2026-07-26 `/grill-me` 已锁定的 17 项决策，遵守 `CONTEXT.md` 的领域词汇与 ADR-0006 至 ADR-0009。对应研究报告 `learn/research/0003-interview-driven-deepening.md` §3.1（P0 效果评估闭环）。

## Problem Statement

Xling 已完成考研焦虑支持闭环切片的全部后端功能（CBT 追问、行动计划、风险轨迹、量表筛查、记忆卡片、人审扩展）和前端适配，也完成了 RAG 增强（多查询、混合检索 RRF、LLM 重排序）。但三个核心模块缺评估：

1. **风险识别没有准确率评测**：`RiskGuardianAgent` 有三层结构（词典快通道 -> LLM JSON 评估 -> 兜底），但没有标注集衡量准确率、召回率、误报率。"漏检可控"目前只是设计意图，不是被验证的事实。
2. **对话质量不可量化**：CounselorAgent 的回复好坏全靠主观感受，没有共情性、安全性、行动落地、边界守持的维度评分。
3. **RAG 增强没有真实对比**：4 种检索策略（baseline / multi-query / hybrid-rrf / llm-rerank）的评测 summary 已生成，但全部在 mock 模式下跑（无真向量检索），baseline 和 multi-query 结果完全一样，无法证明增强有效。

## Solution

交付三个独立评测 runner，分别覆盖风险识别、对话质量和 RAG 检索，把项目从"能演示"升级为"能评测"：

1. **风险识别评测集**：50 条人工编写标注消息（覆盖 HIGH/MEDIUM/LOW 三类，含边界陷阱），用真实 LLM 跑三层风险检测，产出 per-class precision/recall/F1、macro-F1、confusion matrix 和逐条明细。
2. **对话质量 LLM-as-judge**：30 条人工编写场景消息（覆盖焦虑倾诉/低落抑郁/高风险暗示/日常问候/边界陷阱），用真实 LLM 生成 CounselorAgent 回复，再用不同模型当裁判按 4 个维度（共情性/安全性/行动落地/边界守持）打 1-5 分，跑 3 次验证稳定性。
3. **RAG 增强 before/after 对比**：用真实 OpenAI embedding 重跑 4 种检索策略，生成并排对比表（JSON + Markdown），标注最强策略相对 baseline 的提升幅度。

三个评测 runner 各自独立，测试用 mock provider 验证指标计算逻辑，真实评测用真实 LLM 通过环境变量配置。

## User Stories

### RAG 增强 before/after 对比

1. As a developer, I want to re-run the 4 RAG strategies with real OpenAI embeddings, so that the comparison reflects true vector retrieval instead of hybrid_score fallback.
2. As a developer, I want a comparison summary that puts all 4 strategies side by side, so that I can see which strategy improves which metric.
3. As a developer, I want a Markdown comparison table with a delta row, so that I can directly state "rerank improved MRR from X to Y" in interviews.
4. As a developer, I want the comparison runner to read existing summary files when available, so that I don't have to re-run all 4 strategies every time I just need the comparison table.
5. As a developer, I want the RAG eval runner to support switching ai_provider to ollama, so that I can later compare fine-tuned model retrieval quality without changing architecture.

### 风险识别评测集

6. As a developer, I want a labeled dataset of 50 messages with expected risk levels, so that I can measure the accuracy of the three-layer risk detection.
7. As a developer, I want the dataset to include boundary trap cases, so that false positives and false negatives are exposed.
8. As a developer, I want the risk eval runner to call PsychologicalAssessmentService.aassess() with a real LLM, so that the evaluation measures true LLM risk understanding, not just keyword matching.
9. As a developer, I want per-class precision, recall, and F1, so that I can identify which risk level has the worst detection.
10. As a developer, I want a confusion matrix, so that I can see exactly which risk levels are confused with each other.
11. As a developer, I want a per-case detail in the report, so that I can locate and analyze bad cases.
12. As a developer, I want macro-F1 as a single summary metric, so that I have one number to track across runs.
13. As a developer, I want the runner to support mock mode for CI, so that the metric calculation logic is regression-tested without API cost.
14. As a developer, I want the runner to support real LLM mode for actual evaluation, so that I get truthful numbers for interviews.

### 对话质量 LLM-as-judge

15. As a developer, I want a scenario dataset of 30 user messages covering 5 scenario types, so that CounselorAgent replies are evaluated across diverse situations.
16. As a developer, I want the quality eval runner to generate CounselorAgent replies using a real LLM, so that the evaluation measures actual system reply quality.
17. As a developer, I want a judge model different from the generation model, so that self-evaluation bias is avoided.
18. As a developer, I want the judge to score 4 dimensions (empathy, safety, actionability, boundary) on a 1-5 scale, so that reply quality is broken down by aspect.
19. As a developer, I want each dimension's rubric to have clear 1/3/5 anchor descriptions, so that judge scoring is consistent.
20. As a developer, I want the judge to run 3 times per case, so that I can verify scoring stability via standard deviation.
21. As a developer, I want the report to show mean and standard deviation per dimension, so that I know which dimensions are reliably scored.
22. As a developer, I want the judge to use temperature=0, so that scoring is as deterministic as possible.
23. As a developer, I want the generation to use temperature=0.3, so that replies are natural but not overly random.
24. As a developer, I want the runner to support mock mode for CI, so that the scoring and stability calculation logic is regression-tested.
25. As a developer, I want all LLM endpoints configurable via environment variables, so that I can switch between OpenAI, domestic models, and local models.

## Implementation Decisions

### 三个评测目录平行

- 新建 `app/risk_eval/`（风险识别评测）和 `app/quality_eval/`（对话质量评测），与现有 `app/rag_eval/` 平级。
- `app/rag_eval/` 在现有基础上新增对比汇总功能，不迁移现有代码。

### 第一阶段：RAG 增强 before/after 对比

- **真实 embedding 重跑**：在 `app/rag_eval/runner.py` 现有 `evaluate()` 基础上，配置 `rag_eval_ai_provider` 为 `openai`（需设置 `OPENAI_API_KEY`）并确保 `knowledge_vector_enabled=True`，依次跑 baseline / multi-query / hybrid-rrf / llm-rerank 四种策略。runner 代码已支持，无需改动核心逻辑。
- **对比汇总函数**：在 `app/rag_eval/runner.py` 新增 `build_comparison()` 函数，读取 4 份 summary JSON，输出并排对比数据。
- **对比输出两份**：
  - JSON 汇总，包含 4 种策略的 5 个指标（recallAtK / precisionAtK / mrr / ndcgAtK / hitRate）并排，以及最强策略相对 baseline 的 delta。
  - Markdown 对比表，人类可读，含指标列、策略行、delta 行。
- **读取已有 summary**：对比汇总函数优先读取已生成的 summary 文件；若文件不存在则提示先跑对应策略。
- **微调对比预留**：通过修改 `rag_eval_ai_provider` 为 `ollama` 即可接入微调模型重跑，无需改架构。配置项已存在于 `app/core/config.py`。

### 第二阶段：风险识别评测集

- **评测边界**：只评 `PsychologicalAssessmentService.aassess()`，不评 runtime 层的 intent 覆盖和轨迹增强。后者已有单元测试覆盖。
- **标注集**：50 条人工编写，分布为 HIGH 18 / MEDIUM 16 / LOW 16，其中约 8 条为边界陷阱（正常情绪含高风险词测误报、委婉自杀意念不含关键词测漏报、反讽引用测误报）。
- **标注集格式**：JSON 数组，每条含 `id`（引用标识）、`text`（用户消息原文）、`expected_risk`（LOW/MEDIUM/HIGH）、`category`（direct/euphemism/plan/normal_vent/mild_anxiety/trap，分组分析用）、`notes`（可选标注说明）。
- **标注集存放**：`app/risk_eval/xling-risk-eval.json`。
- **runner**：`app/risk_eval/runner.py`，提供 `evaluate()` 函数。读取标注集 -> 初始化 AiClient 和 PsychologicalAssessmentService -> 对每条标注异步调用 `aassess()` -> 收集预测结果 -> 计算指标 -> 输出报告。
- **指标**：per-class precision/recall/F1（LOW/MEDIUM/HIGH 各一组）、macro-F1、3x3 confusion matrix、逐条明细（每条的预测风险 vs 期望风险、是否命中、category）。
- **真实 LLM 模式（默认推荐）**：通过 `RISK_EVAL_AI_PROVIDER` 环境变量配置 provider（openai / ollama），复用系统现有的 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OLLAMA_BASE_URL` 等配置。
- **mock 模式（CI 回归）**：设置 `RISK_EVAL_AI_PROVIDER=mock`，runner 走 `AiClient._mock` 的风险评估分支，验证指标计算逻辑正确性，不产出真实评测结论。
- **输出**：
  - 完整报告 JSON（逐条明细 + 指标 + confusion matrix）。
  - 汇总摘要 JSON（只含指标数字，方便跨次对比）。
- **运行方式**：`python -m app.risk_eval.runner`，可选 `--provider mock` 参数。

### 第三阶段：对话质量 LLM-as-judge

- **评测边界**：评 CounselorAgent 单条回复。构造用户消息 -> 用 CounselorAgent 的 system prompt + 真实 LLM 生成回复 -> judge 打分。
- **场景集**：30 条人工编写，5 类各 6 条：焦虑倾诉（测共情 + 行动落地）、低落抑郁（测共情 + 安全性）、高风险暗示（测安全性是否引导求助）、日常问候（测边界守持不过度心理化）、边界陷阱（测边界守持不给诊断）。
- **场景集格式**：JSON 数组，每条含 `id`、`text`（用户消息）、`category`（anxiety/depressed/risk_hint/casual/boundary_trap）、`notes`（可选）。
- **场景集存放**：`app/quality_eval/xling-quality-eval.json`。
- **评分维度**：共情性（empathy）、安全性（safety）、行动落地（actionability）、边界守持（boundary），每个维度 1-5 分。每个分数档有明确锚点描述（1 分/3 分/5 分的行为定义），写入 judge prompt。
- **judge prompt**：`app/quality_eval/judge_prompt.py`，包含维度定义、分数锚点、输出 JSON 格式约束（4 个维度各一个分数 + 理由）。
- **生成回复**：用系统配置的 provider（`QUALITY_EVAL_GEN_PROVIDER`），temperature=0.3。
- **judge 模型**：用和生成不同的 provider 或模型（`QUALITY_EVAL_JUDGE_PROVIDER` + `QUALITY_EVAL_JUDGE_MODEL` + `QUALITY_EVAL_JUDGE_BASE_URL` + `QUALITY_EVAL_JUDGE_API_KEY`），temperature=0。都走 OpenAI 兼容接口，国产模型通过 base_url 接入。
- **稳定性验证**：对同一批评测跑 3 次 judge（`QUALITY_EVAL_JUDGE_RUNS=3`），报告每个维度的均值 + 标准差。标准差 > 0.5 的维度在报告中标注不稳定。
- **runner**：`app/quality_eval/runner.py`，提供 `evaluate()` 函数。
- **mock 模式（CI 回归）**：用 FakeAiClient 返回 canned 回复和 canned judge 评分 JSON，验证评分解析和稳定性计算逻辑。
- **输出**：
  - 完整报告 JSON（逐条回复 + 4 维度评分 × 3 次 + 均值 + 标准差）。
  - 汇总摘要 JSON（只含维度均分 + 标准差）。
- **运行方式**：`python -m app.quality_eval.runner`，可选 `--provider mock` 参数。

### 配置位置汇总

所有新增配置项加入 `app/core/config.py` 的 `Settings` 类，通过 `.env` 文件设置，与现有配置项风格一致：

- 风险评测：`risk_eval_dataset`、`risk_eval_output`、`risk_eval_summary_output`、`risk_eval_ai_provider`
- 对话质量评测：`quality_eval_dataset`、`quality_eval_output`、`quality_eval_summary_output`、`quality_eval_gen_provider`、`quality_eval_judge_provider`、`quality_eval_judge_model`、`quality_eval_judge_base_url`、`quality_eval_judge_api_key`、`quality_eval_judge_runs`
- RAG 对比：`rag_eval_comparison_output`（JSON 路径）、`rag_eval_comparison_md_output`（Markdown 路径）

### 现有能力复用

- RAG 评测复用现有 `app/rag_eval/runner.py` 的 `evaluate()` 和 4 种策略支持，只新增对比汇总函数。
- 风险评测复用 `PsychologicalAssessmentService.aassess()` 和 `AiClient`（含 mock 分支）。
- 对话质量评测复用 `AiClient` 的 OpenAI 兼容接口和 CounselorAgent 的 system prompt（`PromptTemplates`）。
- 测试复用 `tests/test_assessment.py` 的 `FakeAiClient` 模式和 `tests/test_rag_eval.py` 的纯函数 + 端到端测试模式。

## Testing Decisions

### 测试原则

自动化测试只验证指标计算逻辑的正确性，用 mock provider 注入已知输入和期望输出，不跑真实 LLM。真实 LLM 评测是手动运行，不在 CI 范围内。测试不断言 runner 内部实现细节，只验证外部行为（输入标注集 -> 输出指标是否正确）。

### Seam 1 -- RAG 对比汇总（扩展 `tests/test_rag_eval.py`）

- 测试 `build_comparison()` 函数：用 4 份已知 summary JSON 作为输入，验证输出对比表的结构、5 个指标的并排排列、delta 计算的正确性。
- 纯函数测试，不需要 LLM、数据库或 Chroma。
- Prior art：`tests/test_rag_eval.py` 的 `test_compute_report_aggregation` 和 `test_build_eval_summary_has_five_metrics_and_no_per_case`。

### Seam 2 -- 风险评测 runner（新增 `tests/test_risk_eval.py`）

- 用 FakeAiClient 返回 canned 风险评估 JSON，跑少量已知标注（5-10 条，覆盖三类），验证 per-class precision/recall/F1、macro-F1、confusion matrix 的计算正确性。
- 验证 mock 模式能跑通完整流程并输出结构正确的报告。
- Prior art：`tests/test_assessment.py` 的 `FakeAiClient` 模式 + `tests/test_rag_eval.py` 的端到端 tempdir 模式。

### Seam 3 -- 对话质量评测 runner（新增 `tests/test_quality_eval.py`）

- 用 FakeAiClient 返回 canned 回复和 canned judge 评分 JSON，验证评分解析（4 维度分数 + 理由提取）、稳定性计算（3 次跑的均值和标准差）的正确性。
- 验证 mock 模式能跑通完整流程并输出结构正确的报告。
- Prior art：`tests/test_assessment.py` 的 `FakeAiClient` 模式。

## Out of Scope

- runtime 层的 intent 覆盖和风险轨迹增强的端到端准确率评测（已有单元测试覆盖逻辑正确性）。
- 多轮 CBT 追问流程和完整闭环切片的对话质量评测（第一版只评单条回复）。
- 人工标注大规模对话质量数据集（第一版用 LLM-as-judge）。
- 评测结果自动写入 README 或简历（手动整理）。
- 评测结果可视化看板（第一版只出 JSON 和 Markdown）。
- 模型微调本身（评测闭环完成后，根据评测发现再决定是否微调，属于后续工作）。
- 评测 runner 的 Web UI 或 API 端点（第一版只支持命令行运行）。
- 跨模型/跨 provider 的系统性 benchmark（第一版只支持配置切换，不做自动化对比矩阵）。

## Further Notes

- 执行顺序为 RAG 对比 -> 风险评测 -> 对话质量，按成本从低到高、复杂度从简到繁推进。
- RAG 对比阶段需要配置 `OPENAI_API_KEY` 和 `knowledge_vector_enabled=true` 才能启用真向量检索。当前 4 份 summary 全是 hybrid_score 兜底，重跑后数字会变化。
- 风险评测的 50 条标注集是人工编写，需集中精力一次性完成，确保难度梯度和边界用例质量。
- 对话质量评测的 judge prompt 是核心交付物，rubric 锚点描述直接影响打分一致性，需反复调试。
- 后续微调对比通过修改 `rag_eval_ai_provider` 为 `ollama`（RAG）、`risk_eval_ai_provider` 为 `ollama`（风险）接入微调模型，无需改架构。
- 三个评测 runner 的真实 LLM 评测结果应整理成面试素材，配合 `learn/research/0003-interview-driven-deepening.md` 的面试讲法使用。
