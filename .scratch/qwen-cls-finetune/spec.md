Status: ready-for-agent

# PRD: Qwen 情绪分类器微调替换风险评估第二层

> 本 PRD 固化 2026-07-27 `/grill-me` 已锁定的 7 项决策，遵守 `CONTEXT.md` 领域词汇与现有三层风险评估架构。不重开 ADR-0006 至 ADR-0009 的任何决策。

## Problem Statement

Xling 的风险评估采用"高风险关键词短路 -> 通用大模型 JSON 评估 -> 保守兜底"三层架构。第二层目前依赖通用大模型（云端 API 或基座 Qwen）加提示词输出 5 字段 JSON，存在四个问题：

1. 学生心理倾诉消息必须发往云端 API 才能完成风险评估，**隐私外泄**，不符合校园心理场景的敏感数据期望。
2. 每次评估都消耗云端 token，**持续产生成本**，且通用大模型对校园心理语境理解有限，提示词脆弱。
3. 通用大模型输出 JSON 不稳定，必须挂载 schema 校验、交叉验证、tenacity 有界重试和保守兜底一整套机制才能保证可用，**链路复杂、延迟高**。
4. 现有声称微调的 `xling-qwen2.5-7b-ft` 仓库内**没有任何训练脚本、训练配置或数据转换流水线**，无法证明其经过真正微调，也无法复现。

本项目需要一个本地、专用、稳定的情绪分类器替换第二层，使风险评估在校园本地完成、成本降为零、输出稳定，同时保留第一层关键词铁闸和第三层保守兜底的安全网。

## Solution

用 Qwen2.5-3B-Instruct 作为基座，通过 LoRA（低秩适配）在现有 2400 条校园心理情绪四分类合成数据上微调出一个**只输出情绪标签**（正常/焦虑/低落/高风险）的专用分类器。训练使用 LLaMA-Factory 框架，产物经 LoRA 合并、Q4_K_M 量化为 gguf 后，通过 Ollama 以独立模型名部署，与现有对话模型并存。

分类器只承担情绪标签判断这一最需要语义理解的环节；情绪分数、风险等级、置信度、摘要等其余字段由现有规则映射补齐（复用 `score_for_emotion`、`risk_from_score` 等已有映射）。风险评估三层架构不变：第一层高风险关键词短路原样保留，第三层保守兜底原样保留，只把第二层的主人从"通用大模型 + 5 字段 JSON"换成"微调分类器 + 中文标签 + 规则补齐"。意图分类（CHAT/CONSULT/RISK）不在本次范围，继续沿用现有通用大模型路径。

本项目定位为面试作品，核心交付物是一份可复现的微调全流程（数据切分 -> 训练 -> 合并 -> 量化 -> 部署 -> 接入 -> 评估），以及"基座 zero-shot vs 微调后"在留出验证集上的真实提升数字。

## User Stories

### 风险评估链路

1. As a student, I want my distress messages assessed for risk locally without being sent to a cloud API, so that my sensitive disclosures stay on campus.
2. As a student, I want risk assessment to return quickly, so that my chat is not stalled by cloud API latency.
3. As a student, I want high-risk keywords like "suicide" or "self-harm" to trigger an immediate HIGH risk judgment, so that my safety is never blocked waiting on a probabilistic model.
4. As a student, I want the system to fall back to a conservative risk level if the local classifier is unavailable, so that my safety is not silently downgraded by an outage.
5. As the system, I want the classifier to output only a single emotion label, so that structured-output parsing and JSON retry machinery are no longer needed on the hot path.
6. As the system, I want emotion label, emotion score, risk level, confidence and summary to remain consistent fields downstream, so that risk trajectory, reports and human review consume the same assessment shape as before.

### 训练流水线

7. As a developer, I want to split the 2400-row synthetic dataset into train and validation sets with a single command, so that evaluation is reproducible.
8. As a developer, I want to register the existing Alpaca-format dataset with LLaMA-Factory without reformatting, so that training starts from data I already have.
9. As a developer, I want to launch LoRA fine-tuning on Qwen2.5-3B-Instruct with one LLaMA-Factory command, so that a single consumer GPU is sufficient.
10. As a developer, I want training to keep Chinese labels in the data untouched, so that no data rewriting is needed before training.
11. As a developer, I want to merge the trained LoRA adapter back into the base model, so that I get a standalone full-weight model for export.
12. As a developer, I want to quantize the merged model to Q4_K_M gguf, so that it is small enough for local Ollama deployment.
13. As a developer, I want a quality check on the synthetic data before training, so that mislabeled rows do not corrupt the classifier.

### 部署与接入

14. As an operator, I want the classifier loaded as a distinct Ollama model separate from the conversation model, so that the two roles do not interfere.
15. As an operator, I want the classifier model name to be configurable, so that deployment is not hardcoded.
16. As a developer, I want the classifier to always run locally via Ollama regardless of AI_PROVIDER, so that risk assessment never depends on a cloud key.
17. As a developer, I want the psychological assessment service to call the classifier on the non-keyword path, so that the second layer uses the fine-tuned model.
18. As a developer, I want the legacy general-LLM assessment path preserved as a fallback, so that reverting is possible if the classifier underperforms.
19. As a developer, I want intent classification left untouched, so that this change is scoped to risk assessment only.
20. As a developer, I want the Chinese label mapped to the existing EmotionLabel enum, so that downstream code sees the same typed values.
21. As a developer, I want emotion score, risk level, confidence and summary derived by rules from the label, so that the classifier's single output fills the full assessment.

### 评估与可复现

22. As a developer, I want an evaluation script that runs the base Qwen2.5-3B-Instruct zero-shot on the held-out validation set, so that I have a pre-finetuning baseline.
23. As a developer, I want the same script to run the fine-tuned classifier on the same validation set, so that the before/after comparison is apples-to-apples.
24. As a developer, I want the report to include overall accuracy, per-class precision/recall/F1 and a confusion matrix, so that I can see which emotion pairs are confused.
25. As a developer, I want high-risk class recall called out separately, so that the safety-critical metric is visible.
26. As a developer, I want the evaluation report written to the same target directory style as existing RAG eval, so that it fits the project's reporting conventions.
27. As an interviewer, I want every step from data split through deployment to be reproducible from the repo, so that the claimed improvement can be verified.
28. As an interviewer, I want a single before/after improvement number for overall F1 and high-risk recall, so that the value of fine-tuning is concrete.

### 安全与降级

29. As a developer, I want the high-risk keyword short-circuit to remain the first layer unchanged, so that deterministic safety is never removed.
30. As a developer, I want the conservative safe fallback to remain the third layer unchanged, so that a classifier outage still yields MEDIUM risk, not LOW.
31. As a developer, I want classifier call failures to degrade to the conservative fallback, so that no path silently produces a LOW judgment when the model is down.
32. As a developer, I want the runtime intent==RISK override to still force HIGH regardless of classifier output, so that the existing safety override is preserved.

## Implementation Decisions

- **基座模型**：Qwen2.5-3B-Instruct（指令版）。纯 4 分类任务输入短、输出一个词，3B 足够且推理快、gguf 约 2GB；不选 7B 以避免杀鸡用牛刀并保留"小模型微调逼近大模型"的论点。
- **训练方法**：LoRA 低秩适配，rank 16，target 含 q/k/v/o proj，学习率 1e-4，epoch 4，cutoff 256，bf16，单卡。不选全参数微调以避免灾难性遗忘和高算力需求。
- **训练框架**：LLaMA-Factory。现有 Alpaca 格式数据零改造可直接注册使用，训练-合并-导出一条龙。
- **数据策略**：复用现有 2400 条情绪四分类合成数据，9:1 切分训练/验证（2160/240，每类 60）。标签保持中文（正常/焦虑/低落/高风险）不动，训练前人工抽检约 50 条，重点核查焦虑↔低落边界与高风险误标为低落。本切片不扩数据、不做多任务、不引入意图分类标注。
- **分类器职责边界**：分类器只输出一个中文情绪标签。其余字段全部规则补齐：情绪分数复用 `score_for_emotion`；风险等级由情绪推导（高风险->HIGH、低落->MEDIUM、焦虑->LOW、正常->LOW）；置信度由规则给；摘要用模板补。不要求分类器输出 JSON，不继承 5 字段结构化输出的不稳定问题。
- **标签映射**：新增中文标签到 `EmotionLabel` 枚举的映射（正常->NORMAL、焦虑->ANXIETY、低落->DEPRESSED、高风险->HIGH_RISK）。映射在心理评估服务接入层完成，训练数据本身不改。
- **风险评估三层架构不变**：第一层 `has_high_risk_signal` 关键词短路原样保留，命中即 HIGH 并跳过分类器；第三层 `safe_fallback_assessment` 保守 MEDIUM 兜底原样保留。只替换第二层主人。
- **第二层接入**：心理评估服务在非关键词路径上调用分类器（经 Ollama 本地模型），将返回的中文标签经映射和规则补齐为完整 `PsychologyAssessment`。现有通用大模型 5 字段 JSON 路径（含 schema 校验与 tenacity 重试）保留为代码内降级备用，不删除，但默认不启用。
- **降级链**：关键词短路 -> 微调分类器（主）-> 分类器调用失败 -> `safe_fallback_assessment`（保守 MEDIUM）。不引入"分类器失败再回退通用大模型"的四级链，保持三层结构清晰。
- **AiClient 扩展**：`AiClient` 增加分类器专用模型名与一个简化的分类提示词（只要求输出标签词，不复用要求 5 字段 JSON 的 `psychology_prompt`）。分类器调用与对话调用通过模型名区分。
- **配置项**：新增 `ollama_classifier_model` 配置，默认 `xling-cls-3b-ft:latest`，与现有 `ollama_model`（对话）分开。分类器本地专属，不随 `AI_PROVIDER` 切换；Ollama 不可用时降级到保守兜底。
- **部署**：分类器经独立 Ollama 模型名部署，与对话模型并存。沿用现有 Modelfile + 模型创建脚本的同套路，新增分类器专用的 Modelfile 与量化 gguf 产物。
- **产物链路**：LoRA adapter -> 合并回基座导出完整权重 -> Q4_K_M 量化为 gguf -> Ollama Modelfile 加载。命名体现 3B 与分类用途，不与现有 7B 对话模型命名混淆。
- **意图分类不动**：`_classify` 的 CHAT/CONSULT/RISK 判断继续沿用现有通用大模型 + 关键词三层结构，本切片不触及。
- **runtime 安全 override 不动**：intent==RISK 时强制 HIGH、emotion_score 至少 4.0 的 override 逻辑原样保留，分类器输出不影响该硬提升。

## Testing Decisions

- **测试原则**：只验证外部行为，不断言分类器模型名、提示词原文或内部调用次数。分类器输出通过假 provider 注入，验证情绪标签到完整评估的映射与三层降级行为。
- **主 seam - PsychologicalAssessmentService.assess**：复用现有 seam，不新增。沿用 `test_assessment.py` 的 `FakeAiClient` 注入模式，扩展假客户端使其返回中文标签词，覆盖：四类标签分别映射到正确的 emotion/risk/emotion_score；高风险关键词命中仍短路 HIGH 且不调用分类器；分类器调用失败仍走保守 MEDIUM 兜底。
- **安全行为 seam**：复用 `test_risk_guardian.py` 的 runtime 注入模式，验证 intent==RISK override 仍在分类器输出 LOW 时强制 HIGH，确认本切片未破坏既有安全硬提升。
- **不测训练流水线**：数据切分、LoRA 训练、合并、量化、Ollama 加载不属于单元测试范畴，由评估脚本与人工验证覆盖。
- **评估脚本**：借鉴 `app/risk_eval/runner.py` 的自包含结构，对留出验证集运行基座 zero-shot 与微调后两轮，输出整体准确率、per-class precision/recall/F1、混淆矩阵、高风险 recall 到 `target/cls-eval-report.json`，与现有 RAG 评测产出风格一致。
- **Prior art**：沿用 `tests/test_assessment.py` 的三层用例结构、`tests/test_risk_guardian.py` 的 runtime override 用例、`app/risk_eval/runner.py` 的评测脚本骨架。

## Out of Scope

- 意图分类（CHAT/CONSULT/RISK）的微调；`_classify` 继续用通用大模型。
- 对话模型的微调；学生可见回复仍由现有对话模型（7B 或云端 API）生成。
- 多任务训练（情绪分类 + 意图分类联合）；留作后续第二阶段。
- 数据扩造；本切片只用现有 2400 条合成数据，不新增标注。
- 通用大模型降级链；分类器失败直接走保守兜底，不回退到云端通用大模型。
- 真实学生数据测试集与对抗性边界 case 测试集；本切片用合成留出集，边界 case 测试列为可选加分项。
- 7B 或更大基座；本切片固定 3B，更大尺寸留作效果不足时的备选。
- 训练框架替换为 ms-swift 或 unsloth；本切片固定 LLaMA-Factory。
- 全参数微调；本切片固定 LoRA。
- 论文级多基线对比与统计显著性检验；本切片定位面试作品，只做基座 zero-shot vs 微调后单一核心对比。

## Further Notes

- 本切片不与 ADR-0006 至 ADR-0009 任何决策冲突。ADR-0009 的显式量表筛查不在本切片范围；风险评估三层架构与人审流程均保持原状，只换第二层实现。
- 与 `exam-anxiety-closed-loop` PRD 中"Structured Output：风险评估使用 schema 约束结构化结果 + tenacity 重试"的关系：本切片的分类器路径因只输出标签词而不再依赖 JSON schema 校验与 tenacity 重试，但保守兜底保留；这是对第二层实现方式的替换，不撤销"耗尽后进入安全 fallback"的安全决策。现有通用大模型路径作为代码内备用保留，必要时可回退。
- 现有 `xling-qwen2.5-7b-ft` gguf 仓库内无任何训练脚本或配置，无法证明经过真正微调。本切片是项目首次建立可复现的微调流水线，产出的 3B 分类器与该 7B 文件无继承关系。
- 评估集局限：240 条留出集与训练集来自同一份合成数据，分布偏干净，提升数字偏乐观。汇报与简历表述时应说明评估集来源，不夸大为真实场景表现。
- 简历可表述方向：基于 Qwen2.5-3B-Instruct 用 LoRA 在 2400 条校园心理情绪分类数据上微调，经 LLaMA-Factory 完成训练、合并、Q4_K_M 量化，Ollama 部署接入风险评估链路替换通用大模型；在 240 条留出集上整体 F1 与高风险召回率相对基座 zero-shot 的提升数字待评估脚本产出后填入。
- 高风险 recall 是安全关键指标，若微调后在留出集上该指标显著低于基座 zero-shot，则不上线替换第二层，回退至扩数据或调阈值方案。
