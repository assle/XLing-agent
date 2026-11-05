# 完整风险服务与 RAG 优化验证报告

日期：2026-11-02  
数据集版本：`ind-20261102-v2`  
对话与复核模型：`deepseek-v4-flash-vision-exp`（DeepSeek 兼容接口）  
本地分类器：`xling-general-cls-05b:quoted-f16`  
向量模型：`qwen3.7-text-embedding-flash`

## 结论

- 完整风险服务在冻结独立风险集上为 **35/36，Accuracy 97.22%，Macro F1 97.22%**，达到 90% 目标。
- 当前生产对应的 multi-query + Chroma dense 主检索在冻结独立 RAG 集上为 **29/30，目标来源 Recall@4 96.67%**，达到 90% 目标。
- 未参与调参的 12 题 RAG holdout 为 **11/12，目标来源 Recall@4 91.67%**，同样达到 90%。
- 原 100 题 RAG 回归集中，36 题期望的 Markdown 来源已被拆分删除，不能用于当前来源命中判定；剩余 64 题为 **61/64，目标来源 Recall@4 95.31%**。完整 100 题原始来源命中率也保留为 **61.00%**，没有删除或改写原始结果来隐藏该数字。
- 最终完整非 MySQL 回归：**645 passed**。五个冻结数据文件的 SHA256 与冻结清单逐项一致。

运行条件：当前实现、上述模型标识和本报告列出的数据集。

## ADR 与实现

### 风险服务

- ADR-0023：非自身危险语境下，决策头 HIGH 必须进入完整语境复核，避免否定、影视、新闻或第三人称危险文字直接升级。
- ADR-0024：LOW／MEDIUM 增加持续性与功能受损判断，只允许 LOW→MEDIUM 升级，不降低 HIGH。
- 隐晦即时危险与需要语境复核的 HIGH 使用完整生成式复核；普通低置信样本仍走本地分类器。
- 复核不可用时保持保守安全结果。
- 原端到端回归中 3 条包含明确失眠／睡不着的 LOW 预期已按 ADR-0024 改为 MEDIUM；冻结独立集未修改。

### RAG

- ADR-0025 的通道感知 RRF 实测不足且有安全漏检，已由 ADR-0026 取代，不恢复该方案。
- ADR-0026：原查询与去重改写全部参与；有效向量路径只按 dense 结果合并排序，跨查询保留片段最佳 dense 分数；BM25 仅在向量不可用时作为本地混合兜底。
- 旧 `retrieve_hybrid_rrf` 和 `hybrid-rrf` 评测入口保留为离线对照，不与当前生产 dense 结果混称。
- 新增 `dense-primary` 评测入口，报告标签明确为 `multi-query + Chroma dense primary (local hybrid fallback)`。

## 风险冻结集结果

| 指标 | 结果 |
|---|---:|
| 样本数 | 36 |
| Accuracy | **97.22%** |
| Macro F1 | **97.22%** |
| HIGH Precision | 100.00% |
| HIGH Recall | 100.00% |

混淆矩阵：

| 真实标签 | LOW | MEDIUM | HIGH |
|---|---:|---:|---:|
| LOW | 11 | 1 | 0 |
| MEDIUM | 0 | 12 | 0 |
| HIGH | 0 | 0 | 12 |

唯一不一致为 `ind-risk-low-11`：游戏语境由 LOW 判为 MEDIUM。本轮未再针对该题增加规则。

详细结果：`risk.json`；摘要：`risk-summary.json`。

## RAG 真实检索结果

以下均使用 Top 4、真实 Chroma 向量和 DeepSeek 查询改写。主要采用指标为原 `expectedSources` 的目标来源 Recall@4。

| 数据口径 | 命中／总数 | 目标来源 Recall@4 | 回答相关性 | MRR | nDCG@4 |
|---|---:|---:|---:|---:|---:|
| 冻结独立集 | 29/30 | **96.67%** | 96.67% | 0.9000 | 0.9062 |
| 全新 holdout | 11/12 | **91.67%** | 91.67% | 0.8750 | 0.8832 |
| 原回归完整原始口径 | 61/100 | **61.00%** | 87.00% | 0.8483 | 0.8366 |
| 原回归现行来源可判定子集 | 61/64 | **95.31%** | 96.88% | 0.9609 | 0.9452 |

### 漏检与安全口径

- 冻结集漏检：`ind-rag-campus-02`；5 个安全关键题的目标来源漏检为 0。
- Holdout 漏检：`hold-rag-02`；2 个安全关键题的目标来源漏检为 0。
- 原回归现行来源子集漏检：`risk-professional-support`、`academic-grad-advisor-communication`、`adjustment-graduation-self-worth`。
- 其中 `risk-professional-support` 未命中目标 `risk-policy.md`，因此现行来源子集存在 **1/32 个目标来源安全漏检**。该题通过其他现行资料命中了预期答案词项，所以评测器按“回答相关性”计算的安全漏检率为 0；两种口径不能混写。

### 原回归旧来源不兼容

`seed_data` 只加载当前 `app/knowledge/*.md`。Git 历史确认 `campus-mental-health.md` 和 `counselor-referral-and-resources.md` 已在通用心理健康资料迁移中删除并拆分。原回归中：

- 32 题期望 `campus-mental-health.md`；
- 4 题期望 `counselor-referral-and-resources.md`；
- 合计 36 题标为 `stale-source`，只排除出来源可比 Recall，不删除原结果，也不自动把语义相近的新来源补成正确答案。
- 这 36 题中 25 题仍命中预期答案词项，但由于没有人工审核新增相关来源，不计入目标来源命中。

36 个 stale-source ID 和逐题原始返回保存在 `rag-validation.json`、`rag-regression-dense/combined.json`。可判定子集逐题结果保存在 `rag-regression-dense/comparable.json`。

## 评测有效性与复现说明

- 冻结风险集和 RAG 集均未在本轮修改；五个冻结文件的哈希与 `evals/independent/frozen-manifest.json` 一致。
- Holdout 在 dense 方案确定后才运行，未用于继续调参。
- 初次 dense 复测曾因运行环境的 IPv6 `NO_PROXY` 写法触发 `InvalidURL: Invalid port ':1]'`，DeepSeek 改写静默降级，因此该次结果不作为采用证据。修正运行环境后真实请求成功，最终报告均为 `comparisonEligible=true` 的向量运行。
- 复现命令需要保持 DeepSeek 的 `RAG_EVAL_*` 配置，并在当前机器上使用兼容的代理排除列表：`NO_PROXY=localhost,127.0.0.1`、`no_proxy=localhost,127.0.0.1`。
- 标签来源为 agent-authored；已批准的 oracle 修正单独记录，其余标签的人工复核状态见冻结清单。

## 最终工程检查

已实际执行：

```bash
PYTHONPATH=. .venv/bin/python -m pytest --ignore=tests/test_support_turn_mysql.py -q
# 645 passed in 41.27s

PYTHONPATH=. .venv/bin/python -m pytest tests/test_e2e_eval.py tests/test_hyde_retrieval.py tests/test_rag_eval.py -q
# 59 passed in 11.03s

git diff --check
PYTHONPATH=. .venv/bin/python -m py_compile app/services/ai.py app/services/assessment.py app/services/knowledge.py evals/rag/runner.py
```

`git diff --check`、语法编译和冻结哈希检查均通过。未运行需要 MySQL 的 `tests/test_support_turn_mysql.py`，未执行提交或部署。

## 结果文件索引

- 风险：`risk.json`、`risk-summary.json`
- RAG 冻结集：`rag-dense/rag-dense-primary.json`、`rag-dense/rag-dense-primary-summary.json`
- RAG holdout：`rag-holdout/rag-dense-primary.json`、`rag-holdout/rag-dense-primary-summary.json`
- 原回归合并：`rag-regression-dense/combined.json`、`rag-regression-dense/combined-summary.json`
- 原回归可判定子集：`rag-regression-dense/comparable.json`、`rag-regression-dense/comparable-summary.json`
- 汇总与 stale-source ID：`rag-validation.json`
