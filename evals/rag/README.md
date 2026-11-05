# 分块、混合检索与固定基准

当前线上默认检索为多查询＋Chroma dense 主检索（ADR-0026）：原查询与回答模型生成的最多 3 条改写查询全部参与，跨查询保留每个片段的最佳 dense 分数后合并去重；无模型客户端或改写失败时保留原查询。有效 dense 结果不再叠加 BM25 名次，向量不可用时使用本地混合兜底。旧 `hybrid-rrf` 保留为离线对照，dense＋BM25 min-max 归一化凸组合（向量权重 0.98）保留为 `normalized_fusion` 可选策略，下文基线数据均出自该策略。分块采用标题约束的语义分块，默认 Top-4；仅扩展首位片段，总正文预算 3072 字。BM25 在建索引时预计算词项贡献，以内容为键缓存倒排索引，查询只累加命中项。方法依据与论文实现边界见 [研究依据](retrieval-methods.md)。

语义分块使用现有 embedding 服务编码完整句子，在标题节内部比较相邻句余弦距离；超过该节 75% 距离分位点且当前正文不少于 128 字时允许断开，片段最长 512 字、整句重叠最多 64 字。这不是 late chunking、TextTiling 或 propositionizer 的复现。没有可用向量服务且允许回退时，使用同样保留标题和句界的结构分块与原本地评分。

`knowledge_sources` 仅保存来源正文、分块配置和实际片段的派生指纹，与片段一起提交，避免启动时重复编码未变更的知识；片段缺失或被改动仍触发修复。正常 `create_schema` 会创建该表。内置 Markdown 在 `ensure_source` 时自动检查和重分块；既有上传资料没有原始正文留存，需要重新上传原文才能采用新分块，仅重建向量索引不会重分块。PDF 解析仍为现有 `pypdf` 文本提取。

## 基准与证据边界

基准 `closing-40.jsonl` 包含 38 条可回答、2 条知识不足问题，覆盖 10 个主题，其中 7 条安全相关。问题是工程编写的小样本，预期证据由项目所有者复核，记录在 `closing-gold-review.json`；`closing-deployment-resources.md` 是本人确认使用的虚构资源包，不能用于真实求助或预约。

相关性要求来源一致且返回片段包含该证据要求的全部原文跨度。问题、审核记录和知识正文保持原版本。逐题与汇总报告记录 Recall@4、Precision@4、MRR 和 nDCG@4：Recall 按预期证据项去重，Precision 以 Top-4 为分母，nDCG 使用二元相关性并按当前题预期证据数构造理想排序。本基准每题只有一个预期证据项，因此证据 Recall@4 与 Hit@4 数值相同。两条知识不足题单独统计，不进入可回答题的质量指标均值；拒答行为由独立测试覆盖。

ADR-0020 起，`evals.rag.runner` 另外区分 `targetSourceRecallAtK`（原 `expectedSources` 是否命中）与 `answerRelevanceHitRate`（是否存在可支持回答的结果）。额外 `relevantSources` 只有同时带有 `relevanceReview: {"status":"approved","reviewer":"human","reason":"..."}` 才计入回答相关性；未审核数据标记为 `target-only-unreviewed`，检索器不能为自身结果补标。旧 `hit`、`recallAtK` 等字段保留为回答相关性口径，新旧报告不能只凭同名字段推断同口径提升。

`hybrid-rrf` 详细报告逐题保存 `diagnostics`，包括原查询、改写查询、dense、BM25、融合、合并及最终返回排名，用于区分候选未召回和融合后挤出；轨迹记录不改变线上排序。先依据这些阶段证据选择最小修改，不预设更换模型或调参。离线策略统一通过 `KnowledgeService.retrieve_experiment` 分派，评测 runner 不再组合私有检索方法。当前生产对应的离线验收入口为 `.venv/bin/python -m evals.rag.runner dense-primary`，详细报告和摘要分别默认写入 `target/rag-eval-report-dense-primary.json` 与 `target/rag-eval-report-dense-primary-summary.json`。

使用真实 `qwen3.7-text-embedding-flash` 向量和 Chroma，没有用模拟向量证明质量。模型请求标识不是固定权重身份，远程服务及批次可能改变向量。语义分块探索中观察到一次 65→66 片段变化；后续两次实际入库均为 65 片段、实时检索均命中 38/38，但排名会有波动。入库指纹确保未变更资料不会在每次启动时重新分块。

## 方法对照与采用决定

[完整候选报告](../reports/retrieval-optimization/comparison.json) 比较 35 组配置：固定 512、结构 512、结构 256、语义 512、结构 256 加多命中邻域扩展，各自搭配纯向量、RRF（k=60）和凸组合（向量权重 0.50/0.75/0.90/0.95/0.98）。另保留原本地回退作为对照。不同方法共享真实查询向量、语料、Top-k、候选深度及最大上下文预算；只对候选并集重算余弦，候选召回仍由 Chroma 完成。

| 配置 | Hit@4 | MRR | 安全证据遗漏 |
| --- | ---: | ---: | ---: |
| 固定 512 + 纯向量 | 33/38 | 0.781 | 1/7 |
| 固定 512 + RRF | 30/38 | 0.759 | 3/7 |
| 结构 512 + 纯向量 | 36/38 | 0.901 | 1/7 |
| 结构 256 + 凸组合 0.90 | 37/38 | 0.934 | 1/7 |
| 语义 512 + RRF | 32/38 | 0.822 | 3/7 |
| 语义 512 + 纯向量 | 38/38 | 0.928 | 0/7 |
| **语义 512 + 凸组合 0.98（采用）** | **38/38** | **0.923** | **0/7** |

选择规则在混合候选内依次比较安全遗漏、证据召回、MRR、上下文长度。最终方案是本次候选集中的最佳混合方案；纯向量语义检索的 MRR 略高。高向量权重对应当前中文小语料的词法信号强度。

逐主题留一交叉验证：每轮在其余 9 个主题选择配置，只在留下的主题评分，汇总为 **37/38、MRR 0.917、安全遗漏 1/7**。基准及历史逐题结果已公开使用，权重范围经过探索性扩展，本项记录内部回归与配置稳定性。早期试跑的候选缺失分数置零和安全题不平衡切分已弃用；正式报告补齐两路分数并记录全部候选的负结果。

结构 256 加多命中邻域扩展没有优于相应的首位扩展，反而消耗更多上下文；等权 RRF 也没有胜出。产品中只保留采用方案与必要的无向量回退，不添加实验切换开关。替代分块、融合权重和邻域扩展保留在离线评测入口，便于复现而不进入线上分支。

## 实际路径与效率

[实际验证报告](../reports/retrieval-optimization/verification.json) 使用隔离 SQL 数据库、独立 Chroma 索引、真实 HTTP embedding 和每题新建的服务对象，交替执行基线与新方案。这里的基线保留旧分块与纯向量排序；公共查询代码包含批量读取候选的优化，因此耗时比较不等同于完整旧版本二进制比较。

| 指标 | 原分块与纯向量 | 采用方案 |
| --- | ---: | ---: |
| Hit@4 | 33/38 | 38/38 |
| MRR | 0.781 | 0.925 |
| 安全证据遗漏 | 1/7 | 0/7 |
| 平均返回正文字符数 | 2121 | 1077 |
| 含 HTTP 的检索 p50 / p95 | 345 / 439 ms | 344 / 430 ms |
| 冷入库时间 | 1.98 s | 20.93 s |
| 入库编码文本数 | 30 | 276 |

完整检索时间主要受远程 embedding 影响，小幅网络时延差按单次运行记录。语义分块增加了冷入库成本；相同资料再次检查为 **16.4 ms、0 次 embedding 调用**。本地回退在原分块与新语义分块上分别命中 30/38、31/38，安全遗漏均为 3/7；在线向量路径与本地回退分别报告指标。

在同一 SQL、Chroma、语料、融合与缓存真实查询向量下，仅切换“每次重建 BM25”与“缓存预计算倒排项”，全部 40 题返回排序一致。本地混合检索 p50 **8.58→5.38 ms**，p95 **15.78→8.37 ms**；该消融不含远程耗时。另以重复真实片段构造 65/6500/65000 条合成负载，BM25 阶段中位数分别从 3.13/268.78/3134.09 ms 降至 0.04/7.57/116.37 ms；这些是性能负载，首次倒排建索引时间另列在报告中。索引完整性检查保持启用。

[测试记录](../reports/retrieval-optimization/tests.txt)：577 passed，4 skipped；跳过的是未配置 `MYSQL_TEST_DATABASE_URL` 的 MySQL 事务测试。新增检查覆盖句界和长度、语义断点、来源指纹复用与缺失修复、跨会话入库后的缓存更新、BM25 分数等价、候选并集补分、恒定分数归一化、总上下文预算及失败回退。未对新检索结果重新做回答质量人工评审。

## 复跑

需要现有真实 embedding 凭据。使用新的输出目录，实验缓存保存在该目录的同级 `embedding-cache`，缓存内容为实际服务返回的向量。不要将实验输出指向业务数据库或线上向量目录。

```bash
.venv/bin/python -m evals.rag.optimization \
  --output .scratch/rag-optimization/comparison

.venv/bin/python -m evals.rag.verify_optimization \
  --comparison .scratch/rag-optimization/comparison/comparison.json \
  --output .scratch/rag-optimization/verification
```

两份报告记录来源、代码、数据与审核文件的 SHA256，保留逐题命中、排序和耗时。候选报告中的本地时间使用缓存真实查询向量、不含 HTTP/SQL；实际验证另计真实 HTTP。第二个入口验证当前采用配置，若不同服务运行后选出了其他候选会明确报错，不静默改变线上参数。

## HyDE 离线候选

`hyde-multi-query` 保留原查询并生成与其他多查询组相同的最多 3 条改写，再对原查询和每条改写分别运行“当前查询向量＋HyDE 向量”的 RRF 检索，最后按片段合并去重，不做模型重排或断崖截断。它用于隔离 HyDE 在多查询＋RRF 基础上的收益。

`hyde-rerank` 先用原问题做向量检索，再让回答模型生成一段只用于检索的假设性说明，用“原问题 + 假设性说明”做第二次向量检索；两路结果按 `chunk_id` 去重并用 RRF（`k=60`）融合。随后由现有模型重排接口评分，在至少保留 3 条的前提下寻找首个明显分数落差：绝对落差不小于 0.5 或相对下降不小于 25% 时截断，最终仍不超过请求的 Top-k。

两种策略都不是线上默认能力。假设性说明不会作为检索证据或用户回复。HyDE 生成失败时保留原问题向量结果；向量路径不可用时沿用本地回退。是否采用必须看同一真实模型基准上的安全证据遗漏、Recall、Precision、MRR、nDCG 和额外模型延迟，模拟模式报告只验证流程与输出结构。

```bash
.venv/bin/python -m evals.rag.runner hyde-multi-query
.venv/bin/python -m evals.rag.runner hyde-rerank
```

详细结果分别默认写入 `target/rag-eval-report-hyde-multi-query.json` 和 `target/rag-eval-report-hyde-rerank.json`，摘要写入 `target/rag-eval-hyde-multi-query.json` 和 `target/rag-eval-hyde-rerank.json`。使用真实模型和向量服务的配置方式与其他检索策略一致。

## 历史检索与回答证据

既有 [检索报告](../reports/closing-rag/retrieval.json) 使用固定 512 字、64 字重叠、30 个片段，向量与本地回退分别命中 33/38、30/38；该文件保留原始历史身份。`closing.py retrieval` 现在调用当前入库实现，重新运行不应再被标记为旧固定切片批次。原 `xling-rag-eval.jsonl` 继续用于历史回归。

历史回答对照使用预先选定的 12 题、同一模型请求标识、温度、生成上限和共享部署资源配置，以及冻结的 `closing-answer-rubric.json`。24 个回答均非空且正常结束。本人逐对复核的 [原始判断](../reports/closing-rag/answer-review.json) 与 [汇总](../reports/closing-rag/answer-review-summary.json) 为：有 RAG 较优 4 对、无 RAG 较优 2 对、持平 6 对、双方失败 0 对。评审要求支持建议优先、诊疗作为可选，并避免无关工程细节；虚构号码和预约资源标注测试身份与快照日期。

历史回答原始数据为 [answers.json](../reports/closing-rag/answers.json)，本页的新分块结果单独列示。`answers.review.html` 提供中性 A/B 判断与理由导出；`closing.py answers` 和 `review` 可复跑同类对照。真实回答服务的历史诊断显示 2048 输出预算可能大部分被推理占用，因此历史两组统一采用 4096；报告记录服务返回标识，不保存模型推理正文。
