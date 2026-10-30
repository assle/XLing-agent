# 风险分类器替换调研：Jev 类决策模型与开源安全分类器

- 调研日期：2026-10-30
- 目的：评估是否可以及应该用哪个成熟开源项目替换当前的量化 Qwen 风险分类器，并比较性能效果
- 状态：调研结论，未做任何代码或配置改动

## 1. 当前方案基线（来自仓库内报告）

当前分类器（`finetune/reports/classifier-selection.json`）：

- 基座 Qwen2.5-0.5B-Instruct + LoRA（376 条训练样本），GGUF F16，经 Ollama 本地服务
- 任务：中文心理健康消息**四分类**（正常 / 焦虑 / 低落 / 高风险），高风险 = 自伤、自杀、轻生意念或具体计划
- 160 例合成回归集：accuracy 0.975、macroF1 0.975、高风险召回 1.0、falseHigh 4
- 延迟：P50 约 150 ms、P95 约 175–222 ms（本机 Ollama，描述性测量）
- 输出方式：自回归生成一个标签词，再做文本校验（`evals/classifier/runner.py`）

替换时的硬性门槛建议沿用现有验收口径：**高风险召回 = 1.0**、macroF1 不显著回退、延迟在本机硬件上实测。

## 2. 用户提到的 Jev 是什么

Jev 是 TypeSafe AI（前 OpenAI 研究员 Diogo Almeida 联合创办）2026 年 9 月发布的 "System One" 决策模型：**闭源、仅托管 API**。输入 state + 带类型的问题（choice / score / noul），单次前向返回选项概率分布，不做逐 token 生成；官方宣称 70–500 ms 延迟、按输入 token 计费。它代表的不是某一个模型，而是一个新品类——"决策模型"：标签在请求时定义、输出自带校准概率、无自由文本解析。

- 来源：[TypeSafe unveils decision-focused AI model Jev (Arabian Post)](https://thearabianpost.com/typesafe-unveils-decision-focused-ai-model-jev/)、[What Is Jev? (MindStudio)](https://www.mindstudio.ai/blog/what-is-jev-classifier-model)

Jev 本身不可自托管；但其开源同类在 2026 年 9–10 月密集出现，其中**大厂企业开发**的已有 Cloudflare、AWS、Perplexity 三家。

## 3. 重点候选：企业开发的决策模型（与 Jev 同范式）

### 3.1 Cloudflare Clef / Clef-flash —— 综合首选

- 发布：2026-10-01，Cloudflare Workers AI 团队首批自训模型，**Apache 2.0**，权重在 Hugging Face（`Cloudflare/clef`、`Cloudflare/clef-flash`）
- 架构：冻结 Qwen 骨干（Clef = Qwen3.8-27B，Clef-flash = Qwen3.5-9B）+ rank-256 LoRA + joint schema 决策头；**prefill-only 单次前向**，对所有问题、所有选项并行打分，零文本生成、零输出解析
- 输出：每个选项的概率 + confidence，与 Jev `/v1/systemone` API 完全兼容；64K 上下文，支持文本/JSON/图像/视频（10-09 又发布 Clef-omni 支持音视频）
- 官方基准：BANKING77 分类 macro-F1 94.20（Jev 79.74）；Clef-flash 中位延迟 38.8 ms；Jev Decision Index 0.2.1 领先；**模型卡测试硬件为单张 H200**
- 部署：SGLang 官方容器（`lmsysorg/sglang:dev-clef`）；社区报道 Ollama v0.35.1 已原生支持 Clef `/decision` 接口；Workers AI 托管（Clef-flash $0.038/M 输入 token）可零部署先试
- 生产背书：已在 Cloudflare Threat Intelligence 生产使用；配套 RL 微调平台
- 局限：训练数据与管线未公开（开放权重非可复现开源）；9B/27B 比当前 0.5B 大一个数量级，官方延迟是 H200 数据，**本机延迟必须实测**
- 来源：[Cloudflare 官方博客](https://blog.cloudflare.com/clef-decision-models)、[架构与基准转述（CSDN）](https://blog.csdn.net/gitblog_00141/article/details/168425296)、[Clef vs Strands 分析（beri.net）](https://www.beri.net/article/cloudflare-clef-amazon-strands-decider-open-weight-decision-models-vs-jev-benchmarks-pricing)、[open-llm-radar 模型页](https://open-llm-radar.com/models/clef-flash)

### 3.2 AWS Strands Decider 2B —— 小模型 / 可复现 / 本机实验首选

- 仓库：[strands-labs/strands-decider](https://github.com/strands-labs/strands-decider)，**742 星**（GitHub API，2026-10-30），Apache 2.0，创建 2026-09-29，持续活跃（最近 push 2026-10-29）
- 架构：Qwen3.5-2B-Base 去掉 LM 头，换约 100 万参数的 pointer head + rank-16 LoRA；单次前向，选项标签完全由请求定义，三种题型（noul / choice / score）共享一个 masked softmax
- 校准：官方评估称短分类任务上 confidence ≥ 0.9 的回答约 95% 正确——可对低置信度样本回退人工或大模型
- 透明度最高：**权重、246,678 行训练数据（含逐来源许可证）、训练脚本、逐版本研究记录全部公开**；RTX 3090 约 11 小时可复刻完整训练；明确支持 Apple silicon Mac serving
- 部署：`strands-decider serve` 直接起 `/v1/systemone` HTTP 服务（Jev 兼容）；另有 Gemma 4 底座 E2B–26B 全系列，E2B 可在 CPU 上跑
- 基准：v1 JevBench 180/231（与 Clef 不在同一口径，勿直接比分）
- 局限：2B 通用理解力弱于 9B+；官方示例延迟因硬件而异（README 示例 489 ms 为特定环境，不代表 H100 数据）
- 来源：[GitHub README（含架构、评估、复现说明）](https://github.com/strands-labs/strands-decider)、[AWS Builder 训练文章](https://builder.aws.com)

### 3.3 Perplexity pplx-decider-v1.1-27b

- Apache 2.0，Qwen3.8-27B 微调，多模态；模型卡报 Jev Decision Index 61.56（Jev 57.9）；也可走 Perplexity Decisions API / OpenRouter
- 27B 对当前场景偏重，作为第三选择记录
- 来源：[awesome-decision-models 清单](https://github.com/AnotiaWang/awesome-decision-models/blob/main/README.md)

## 4. 社区决策模型（非大厂，供参考）

| 项目 | 背景 | 规模/延迟 | 关键局限 | 数据（2026-10-30） |
| --- | --- | --- | --- | --- |
| [Laya](https://huggingface.co/convaiinnovations)（Convai Innovations） | 印度初创，Apache 2.0，权重+pip 包 | ModernBERT-large 421M / mmBERT-base 322M 多语言（含中文）；T4 32.8–39.5 ms；MLX 移植 M3 Max 7–14 ms | **官方模型卡明示零样本差**（typed-decisions 0.362，低于多数类基线 0.461），定位"可快速特化的底座"，需微调（`laya-train`，Kaggle 双 T4 4–5 h） | 社区移植 [mizorewww/laya-mlx](https://github.com/mizorewww/laya-mlx) 7,039 星 |
| [OpenJev](https://github.com/razorback16/openjev)（razorback16） | 个人项目，Apache 2.0，Jev 线协议兼容 | DiffusionGemma 26B-A4B；JevBench 81.8%，p50 约 246 ms | 需 24 GB GPU（NVFP4）或 16 GB Mac（MLX 4bit）；个人维护 | 763 星 |
| [simple-jev](https://github.com/featherless-ai/simple-jev)（Featherless AI） | 推理服务商，Apache 2.0 | 把**任意 HF 开放模型**变成单次前向分类端点：读 next-token logits + 共享前缀 KV cache 复用 | 是推理封装而非专用训练模型，精度取决于所选底座 | 617 星 |
| Verdict（Heman10x） | 个人项目 | ModernBERT-base 151M + GLiClass 头 | 个人维护、生态小 | 115–305 星 |

值得注意：simple-jev 的路线可以**直接复用现有 Qwen 权重**，把"生成一个标签词"改为"读标签 logits"，不改模型只改推理方式，是延迟与概率输出的低成本改法，但精度不会因此提升。

## 5. 传统企业安全分类器：为什么不直接适合本任务

用户要求的"成熟企业开发、好评高"项目里，传统安全 guard 模型是另一类，但它们输出的是**固定危害类别（safe/unsafe 等）**，而非本任务的情绪四分类：

| 模型 | 企业 | 规模 | 许可证 | 与本任务的匹配度 |
| --- | --- | --- | --- | --- |
| [Qwen3Guard](https://github.com/QwenLM/Qwen3Guard)（Gen / Stream） | 阿里 | 0.6B / 4B / 8B | Apache 2.0 | 中文强、0.6B 第三方实测完整响应约 132 ms（A10G）；但仅输出 Safe/Controversial/Unsafe + 固定危害类别，**无法直接输出焦虑/低落**；可单独用作"高风险"安全闸门 |
| Llama Guard 4 / Prompt Guard 2（[PurpleLlama](https://github.com/meta-llama/PurpleLlama)，4,450 星） | Meta | 12B / 86M·22M | Llama 社区许可 | 固定 14 类危害，英文为主；Prompt Guard 2 只检测提示注入/越狱，与本任务无关 |
| ShieldGemma | Google | 2B / 9B | Gemma 条款 | 4 类危害、英文训练数据为主 |
| [Granite Guardian](https://github.com/ibm-granite/granite-guardian) | IBM | 8B | Apache 2.0 | 英文为主，187 星 |
| Shieldstral 1.0 | Mistral | 3B | Apache 2.0 | 支持自然语言自定义策略、含中文等 12 语言，文本安全均值 F1 84.9 对标 20B 模型；但仍是"内容是否违反策略"范式，非多标签情绪分类 |
| Nemotron 3.5 Content Safety | NVIDIA | 4B | OpenMDW + Gemma 条款 | 固定安全类别 |

结论：若目标是**整体替换四分类器**，应选第 3 节的决策模型；若只想加强"高风险"检测，Qwen3Guard-0.6B 可作为额外的独立安全层，与情绪分类器串联而非替换。

- 来源：[Qwen3Guard-Stream 模型卡（ModelScope）](https://www.modelscope.cn/models/Qwen/Qwen3Guard-Stream-0.6B)、[Qwen3Guard 三尺寸实测（CSDN）](https://blog.csdn.net/weixin_29041443/article/details/157189195)、[Shieldstral 基准（mer.vin）](https://mer.vin/news/shieldstral-mistrals-3b-open-weights-moderation-model/)、[生产护栏模型综述（Slash Digital，2026-09）](https://slash-digital.io/en/insights/guardrails-production/)

## 6. 对当前项目的适配性判断

收益（决策模型范式 vs 当前生成式分类）：

1. **概率/置信度输出**：可对"高风险"设概率阈值、低置信度回退，比当前"生成一个标签词"更适合安全场景的分层策略
2. **无自回归生成**：消除输出格式漂移与解析分支；共享前缀多次提问可复用（如同时问情绪类别 + 风险是否紧迫）
3. **标签请求时定义**：零样本即可试四分类，不必先走 LoRA 训练流程

风险与未知项：

1. **硬件差异是最大变量**：当前 0.5B 在本机已 P50 150 ms；Clef-flash 官方 38.8 ms 是 H200 数据，9B 在本机（Apple silicon / CPU）很可能**更慢**。Strands Decider 2B 与 Laya 才是本机体积友好的选项
2. **中文情绪零样本精度未验证**：各决策模型训练数据以英文为主，Qwen 底座提供中文能力但不保证中文心理情绪四分类的零样本表现；高风险召回必须重新实测，不达标则需用现有 376 条训练集微调（Strands 配方全公开，Laya 提供微调 CLI）
3. **品类很新**：决策模型品类 2026 年 9 月才出现，无长期生产口碑；大厂背书（Cloudflare 生产自用、AWS 全配方公开）可部分对冲
4. 临床安全口径不变：任何替换以高风险召回 1.0 为门槛，且现有评测集为合成小样本，结论外推有限

## 7. 建议的最小验证步骤（待选择，未实施）

1. **本机零样本对比（半天级）**：`strands-decider serve` 本地起服务，在 `evals/classifier/runner.py` 增加一个 provider，把现有 system prompt 的四类定义映射为一个 `choice` 问题；在同一 160 例回归集 + natural-v12 数据集上对比 macroF1、高风险召回、falseHigh、P50/P95
2. **有条件再测 Clef-flash**：有 GPU 用 SGLang 容器，无 GPU 先用 Workers AI 托管验证精度，精度显著更优再讨论部署成本
3. **零样本不达标再微调**：优先 Strands Decider（训练配方与数据全公开，3090 级硬件可复现），或 Laya-multilingual（最小最快但必须先微调）
4. 全程不改动现网分类路径，仅在 evals 中对比；是否替换由对比数据决定

## 8. 快速选型表

| 需求 | 推荐 |
| --- | --- |
| 大厂背书 + 基准最强 + 有 GPU | Cloudflare Clef-flash |
| 本机小模型 + 可复现训练 + 中文 Qwen 底座 | AWS Strands Decider 2B |
| 极致小快 + 愿意微调 | Laya-multilingual |
| 不改模型、只改推理拿概率 | simple-jev 复用现有 Qwen |
| 只加强高风险安全检测（串联不替换） | Qwen3Guard-Gen-0.6B |
