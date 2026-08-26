Status: ready-for-agent

# 01: SQLite eval baseline + OpenAI provider

## What to build

让 RAG 评测自包含可跑，产出基线数字。当前 eval runner 依赖 MySQL（不可用），改为用 SQLite engine + seed 知识库，不依赖外部 DB。同时配置 OpenAI provider（gpt-4o-mini 做 LLM、text-embedding-3-small 做 embedding），让向量检索路径可用（不只是 hybrid 兜底）。

跑通后保存基线 5 指标（Recall@K / Precision@K / MRR / NDCG@K / HitRate）到单独文件，作为后续每个增强的对比基准。

来自 grilling 决策：SQLite 评测（自包含可重复）+ OpenAI 单 provider（零代码改动，基线测真向量路径）。

## Acceptance criteria

- [x] eval 用 `python -m app.rag_eval.runner` 能跑，不依赖 MySQL/Docker 在运行
- [x] SQLite engine 用于 knowledge_chunks 表 + seed 知识库（读 `app/knowledge/*.md` 切块入库）
- [ ] OpenAI 向量路径工作（Chroma + text-embedding-3-small），不是只走 hybrid 兜底
- [x] 基线 5 指标保存到文件（如 `target/rag-eval-baseline.json`）
- [x] 基线数字有意义（非全零，60 个用例有命中）

## Blocked by

None - can start immediately

## Comments

### 2026-07-15 实现完成（向量路径待环境验证）

实现内容：
- `app/core/config.py`：新增 `rag_eval_database_url`（默认 `sqlite:///data/rag-eval.db`）、`rag_eval_chroma_persist_dir`/`rag_eval_chroma_collection_name`/`rag_eval_chroma_snapshot_dir`（隔离的 eval Chroma 目录）、`rag_eval_baseline_output`（默认 `target/rag-eval-baseline.json`）。
- `app/core/bootstrap.py`：`create_schema(engine=None)` 与 `seed_data(db, settings=None)` 改为可选注入，向后兼容（main.py 不变）。
- `app/rag_eval/runner.py`：自建 SQLite engine（不碰 MySQL），用 eval settings（Chroma 指向隔离目录）seed + retrieve；抽出 `compute_report` / `build_eval_summary` 供 issue 02-04 复用；同时写全量 report（`target/rag-eval-report.json`）和 5 指标 baseline（`target/rag-eval-baseline.json`）。向量路径不可用时打 warning 并标 `retrieval=local hybrid_score`。
- `tests/test_rag_eval.py`：19 个测试（纯指标 ndcg/is_relevant/evaluate_case、聚合、baseline 写文件、SQLite engine wiring、`_retrieval_label`、hybrid 端到端）。全绿。
- `.gitignore` / `.env.example`：补 eval chroma 目录与配置项。

hybrid 兜底基线（已跑通，`python -m app.rag_eval.runner`，无 MySQL/Docker/key）：
`recallAtK=1.0  precisionAtK=0.704  mrr=0.932  ndcgAtK=0.928  hitRate=1.0`（60 用例全命中）。

向量路径 criterion 未勾选的原因（环境限制，非代码问题）：
- 本机 venv 是 Python 3.14.6，`chromadb==0.5.23` 的传递依赖 `tokenizers` 无 3.14 wheel，源码构建（maturin/cargo）失败，装不上。
- 环境也无 `OPENAI_API_KEY`。
- 代码侧已就绪：`KnowledgeService.retrieve` 默认先走 Chroma+text-embedding-3-small，`_retrieval_label` 在 `can_embed=True` 时返回 primary label（有单测覆盖）。在装得上 chromadb 且配了 key 的环境跑 `python -m app.rag_eval.runner` 即得真向量基线（`retrieval=Chroma + OpenAI text-embedding-3-small`）。届时再把此 criterion 勾上并用真向量数字覆盖 baseline 文件。
