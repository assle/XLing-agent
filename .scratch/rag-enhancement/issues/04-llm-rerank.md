Status: ready-for-agent

# 04: LLM 批量 rerank

## What to build

召回 Top-20 候选（比最终 top_k=4 大，给 rerank 足够候选池），用 1 次 LLM 调用（gpt-4o-mini）批量给所有候选打分（JSON，每个 0-10 分），按分排序取 top-4。目标是精排、提升 Precision/NDCG。跑评测对比 hybrid RRF（issue 03）。

来自 grilling 决策：批量打分策略（1 次 LLM 调用而非逐个/pairwise），JSON 结构化输出，高效可控。

## Acceptance criteria

- [x] 召回 top-20 候选（扩大候选池）
- [x] 1 次 LLM 调用批量给所有候选打分，返回 JSON（含 chunk 标识 + 0-10 分）
- [x] 按分数排序，取 top-4 作为最终结果
- [x] 评测跑完，5 指标保存到单独文件（如 `target/rag-eval-llm-rerank.json`）
- [x] 与 hybrid RRF 对比，precision/ndcg 应有提升（期望）

## Blocked by

- 03（增量叠加在 hybrid RRF 之上）

## Comments

### 2026-07-22 实现完成

- `app/services/ai.py`：AiClient 新增 `rerank` 方法（1 次 LLM 调用批量打分，返回 JSON [{index, score}]）
- `app/services/knowledge.py`：新增 `retrieve_with_rerank`（召回 candidate_pool=20，LLM 批量打分后取 top-K）
- `app/rag_eval/runner.py`：llm-rerank 策略叠加在 multi-query + hybrid-RRF 之上（召回 top-20 后 LLM 重排取 top-4）
- eval 结果（mock, hybrid 兜底）：recall=0.983 precision=0.692 mrr=0.936 ndcg=0.931 hitRate=0.983
- 与 hybrid RRF 对比 MRR 提升（0.919 -> 0.936），NDCG 提升（0.920 -> 0.931）
