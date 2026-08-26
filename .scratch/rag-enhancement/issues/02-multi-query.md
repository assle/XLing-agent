Status: ready-for-agent

# 02: Multi-query 检索

## What to build

用 LLM（gpt-4o-mini）把学生问题改写成 3 个子查询，各自走向量检索召回 top-K，按 chunk_id 去重后合并。目标是扩大召回面、提升 Recall。跑评测对比基线（issue 01），看 recall 是否提升。

来自 grilling 决策：先做 multi-query（零新依赖，用现有 LLM），作为 recall 优化的第一步。

## Acceptance criteria

- [x] 每个问题生成 3 个子查询（LLM 驱动，适合检索知识库的表述）
- [x] 每个子查询独立向量召回，结果合并
- [x] 按 chunk_id 去重（最终结果无重复 chunk）
- [x] 评测跑完，5 指标保存到单独文件（如 `target/rag-eval-multi-query.json`）
- [x] 与基线对比，recall 应有变化（期望提升）

## Blocked by

- 01（需要基线数字做对比）

## Comments

### 2026-07-22 实现完成

- `app/services/ai.py`：AiClient 新增 `generate_sub_queries` / `rerank`，PromptTemplates 新增 `sub_query_prompt` / `rerank_prompt`，mock provider 支持子查询改写和批量打分
- `app/services/knowledge.py`：KnowledgeService 新增 `retrieve_multi_query`（接受 `base_retrieve` 参数），3 子查询各自检索后按 chunk_id 去重合并
- `app/rag_eval/runner.py`：strategy 参数 + `_build_retrieve_fn` / `_strategy_paths` / `_strategy_label`
- eval 结果（mock, hybrid 兜底）：recall=1.0 precision=0.704 mrr=0.932 ndcg=0.929 hitRate=1.0
