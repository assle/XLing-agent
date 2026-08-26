Status: ready-for-agent

# 03: Hybrid RRF 混合检索

## What to build

向量检索 + BM25 关键词检索**并行**召回，用 RRF（Reciprocal Rank Fusion）融合排序。当前是"向量失败才走 hybrid 兜底"的串行回退，改为两者并行 + RRF 融合，取长补短（向量抓语义、BM25 抓关键词）。跑评测对比 multi-query（issue 02）。

来自 grilling 决策：装 rank_bm25（pip 秒装），RRF 公式 `score = sum(1/(k+rank))`，k=60（标准值）。

## Acceptance criteria

- [x] `rank_bm25` 已安装，BM25 索引从知识切块构建
- [x] 向量 + BM25 并行检索（不是串行回退）
- [x] RRF 融合（k=60）产出合并排序
- [x] 评测跑完，5 指标保存到单独文件（如 `target/rag-eval-hybrid-rrf.json`）
- [x] 与 multi-query 对比，指标有变化

## Blocked by

- 02（增量叠加在 multi-query 之上）

## Comments

### 2026-07-22 实现完成

- `requirements.txt`：新增 `rank_bm25>=0.2.2`
- `app/services/knowledge.py`：新增 `_retrieve_bm25`（BM25Okapi 从知识切块构建索引）和 `_rrf_fuse`（RRF k=60），`retrieve_hybrid_rrf` 并行向量+BM25 后 RRF 融合
- `app/rag_eval/runner.py`：hybrid-rrf 策略叠加在 multi-query 之上（每个子查询走 hybrid RRF）
- eval 结果（mock, hybrid 兜底）：recall=0.983 precision=0.692 mrr=0.919 ndcg=0.920 hitRate=0.983
- 与 multi-query 对比指标有变化（BM25 引入不同排序信号）
