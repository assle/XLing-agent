# 固定小样本 RAG 对照

当前基准为 `closing-40.jsonl`：38 条可回答、2 条知识不足，预先选定 12 条进行回答对照。问题为工程编写的小样本；预期证据由项目所有者复核，记录在 `closing-gold-review.json`。`closing-deployment-resources.md` 是本人确认使用的虚构测试资源包，不能用于真实求助或预约。

两条检索路径使用相同知识、512 字切片、64 字重叠和 Top-4。向量路径使用实际 embedding 服务及 Chroma，本地路径使用现有本地混合评分。两者都使用现有相邻片段扩展。相关性要求命中指定来源和冻结的全部证据片段，不能仅凭来源名称算命中。

成功运行的结果见 [retrieval.json](../reports/closing-rag/retrieval.json)：两路各 40 次调用无失败，向量索引完整包含 30 个片段。

| 指标 | 向量检索 | 本地回退 |
| --- | ---: | ---: |
| Hit@4（38 条可回答） | 33/38，86.8% | 30/38，78.9% |
| MRR（38 条可回答） | 0.781 | 0.726 |
| 安全相关证据遗漏（7 条） | 1/7 | 3/7 |

收尾评价相同基准上的相对提升，不要求百分之百命中。检索遗漏是指标的一部分；接口错误和索引不完整会使批次不具备可比较条件。两条知识不足问题单独统计，不进入命中率分母。这个对照比较现有检索路径，不单独归因于某次知识更新，也不代表完整产品或临床效果。

回答对照使用同一模型请求标识、温度和生成上限；两组共享相同部署资源配置，仅检索上下文不同。使用固定的 `closing-answer-rubric.json` 和中性 A/B 复核页面。空回答或未正常结束的回答使批次无效，不进入效果比较；回答效果以实际本人评分为准，检索提升不能代替回答提升。

当前完整批次 [answers.json](../reports/closing-rag/answers.json) 的 24 个回答均非空、正常结束。本人逐对复核的 [原始判断](../reports/closing-rag/answer-review.json) 和 [汇总](../reports/closing-rag/answer-review-summary.json) 为：有 RAG 较优 4 对、无 RAG 较优 2 对、持平 6 对、双方失败 0 对。这个小样本支持有限的回答收益，不能解释为普遍有效或临床改善。评审还指出支持建议应优先、诊疗作为可选，并避免在普通支持回复中带入无关工程细节；涉及虚构号码及预约资源时仍需明确其测试身份和快照限制。

## 复跑

需要现有真实回答及 embedding 凭据。输出目录必须是新目录，不写业务数据库或在线向量库。

```bash
.venv/bin/python -m evals.rag.closing retrieval \
  --review evals/rag/closing-gold-review.json \
  --resource-package evals/rag/closing-deployment-resources.md \
  --output .scratch/rag-replay

AI_MAX_TOKENS=4096 .venv/bin/python -m evals.rag.closing answers \
  --retrieval-report .scratch/rag-replay/retrieval.json \
  --output .scratch/rag-replay/answers.json

.venv/bin/python -m evals.rag.closing review \
  --answers-report .scratch/rag-replay/answers.json \
  --review .scratch/rag-replay/answer-review.json \
  --output .scratch/rag-replay/answer-review-summary.json
```

`answers.review.html` 提供逐对判断和理由的导出入口。回答批次记录服务返回的模型标识、结束原因及 token 使用量；不保存模型推理正文。请求标识和服务返回标识不等同于固定权重身份。当前回答服务的诊断表明，2048 的输出预算可能大部分被推理占用，所以本轮两组统一使用 4096；费用和耗时随模型及生成长度变化。

原 `xling-rag-eval.jsonl` 保持历史回归身份。这个 40 条小基准不替代原产品任务要求的至少 100 条及完整策略验收。
