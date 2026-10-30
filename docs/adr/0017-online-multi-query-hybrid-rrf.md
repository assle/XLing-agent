---
status: accepted
---

# 在线默认检索切换为多查询＋混合 RRF

## 背景

ADR-0013 在 2026-10-29 的五种策略同库实测中，多查询＋混合 RRF 取得最高 Hit@4（90% vs 归一化加权基线 87%）、MRR 与 NDCG，并消除本轮安全相关样本遗漏（0/32 vs 1/32），但 P95 从 320 ms 增至 3.89 s，因此当时仅作为展示与对照方案，未替换在线默认。

2026-10-30 项目所有者评估后明确接受该延迟代价，要求将在线默认检索改为多查询＋混合 RRF。本决策取代 ADR-0013 中"不替换当前在线默认检索"的部分，其实测数据与对照结论保持不变。

## 决策

- 新增 `knowledge_retrieval_strategy` 配置：`multi_query_hybrid_rrf`（默认）与 `normalized_fusion`（原 dense＋BM25 min-max 归一化加权，保留为可回退选项）。
- 默认策略与评测口径一致：复用现有 `retrieve_multi_query(query, base_retrieve=retrieve_hybrid_rrf)` 组合——回答模型生成 3 条改写查询，每条查询的向量与 BM25 各取 Top-k 三倍候选按 RRF（k=60）名次融合，多查询结果按片段编号合并、保留最高分。
- 生产装配 `AgentRuntimeDependencies.create` 向 `KnowledgeService` 传入同一 `AiClient`，此前在线构造不带模型客户端，多查询会退化为单查询。
- 降级链：无模型客户端或远程改写失败（新增 `knowledge.multi_query` 降级事件）→ 单查询混合 RRF；向量不可用 → 本地 `hybrid_score` 备用路径（不变）。检索不因改写失败而整体失败。
- 管理状态接口 `primaryRetrieval` 按配置显示实际策略标签；不新增数据库表、公开接口或检索参数。

## 后果

在线检索获得本轮实测的召回收益（Hit@4 +3 个百分点、本轮安全遗漏消除），同时每次检索增加一次远程多查询改写，P95 约 3.9 秒的延迟被明确接受。该收益只对应 ADR-0013 的数据、模型与参数，不外推；若延迟或远程可用性成为问题，可通过配置即时切回归一化加权，无需改代码。

LLM 重排与 HyDE 组合仍为对照入口，本决策不启用它们。知识入库、分块、上下文预算与首位片段扩展逻辑不变。

## 验收

`tests/test_retrieval_strategy.py` 覆盖默认策略值、默认分派以混合 RRF 为底层检索、归一化加权可选、无客户端单查询降级、改写失败降级、多查询去重合并与状态标签；检索相关回归（`test_knowledge_optimization.py` 等）保持通过，其中专测归一化融合路径的用例显式固定 `normalized_fusion` 策略。

## 相关

- [ADR-0013](0013-advanced-retrieval-strategy-evaluation.md)（实测数据与原始决策）
- [ADR-0012](0012-langgraph-runtime-and-local-diagnostics.md)
- 评测原始报告：`evals/reports/advanced-retrieval/`
