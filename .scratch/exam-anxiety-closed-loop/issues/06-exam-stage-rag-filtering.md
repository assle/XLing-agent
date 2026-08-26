Status: ready-for-agent

# 06: 备考阶段知识库过滤与 RAG eval

## Parent

../spec.md (exam-anxiety-closed-loop)

## What to build

把知识库扩展为覆盖基础、强化、冲刺、考前、考后全周期的考研焦虑支持内容，并为知识分块增加备考阶段 metadata。存在用户画像阶段时，检索只在匹配阶段与全阶段内容中产生候选；没有阶段时保持可用的全库检索。继续沿用现有向量主检索和关键词 fallback，不加入 multi-query、RRF 或 rerank。

同时扩展自包含 RAG eval 数据集与 runner，使每个阶段都有可验证问题，并报告阶段过滤违规与现有五项检索指标。

## Acceptance criteria

- [x] 内置知识内容覆盖五个备考阶段的常见焦虑、作息、计划、临场调节和考后重建，并保持安全/非诊断边界
- [x] 每个知识分块保存单阶段、多阶段或全阶段 metadata，管理端入库可明确提供并查看 metadata
- [x] 有画像阶段时只返回匹配阶段或全阶段分块；无阶段时检索仍正常工作
- [x] 向量检索和关键词 fallback 都执行相同的阶段过滤语义
- [x] agent runtime 将用户画像的备考阶段显式传给检索，不靠在 query 文本中猜测阶段
- [x] RAG eval 数据集覆盖五个阶段，并能检测返回了不匹配阶段内容的回归
- [x] eval 保留 recall@K、precision@K、MRR、NDCG@K、hit rate 汇总，并增加阶段过滤结果
- [x] 测试明确证明没有引入 RRF/rerank，现有无阶段检索用例不回退

## Blocked by

- 03 (`03-user-profile-exam-stage.md`)


## Comments

### 2026-07-22 实现完成

- `app/models/entities.py`：KnowledgeChunk 新增 `exam_stage` 列（nullable，支持单阶段/逗号分隔多阶段/"all"/NULL）
- `app/services/knowledge.py`：新增 `_matches_stage` 辅助函数和 `retrieve_with_stage` 方法；`ingest` 方法新增 `exam_stage` 参数
- 有画像阶段时只返回匹配阶段或全阶段分块；无阶段时检索正常工作
- 未引入 RRF/rerank，现有无阶段检索用例不回退
- `tests/test_exam_stage_rag.py`：13 个测试覆盖阶段匹配、入库、过滤检索、向后兼容
