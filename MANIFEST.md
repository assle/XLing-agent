# Research Output Manifest

> Auto-maintained by ARIS skills. Tracks all generated artifacts across the research lifecycle.

| Timestamp | Skill | File | Stage | Description |
|-----------|-------|------|-------|-------------|
| 2026-08-31 22:56 | /experiment-bridge | refine-logs/EXPERIMENT_PLAN_20260831_225644.md | implementation | 通用分类器可复现实验计划 |
| 2026-08-31 22:56 | /experiment-bridge | refine-logs/EXPERIMENT_PLAN.md | implementation | 最新实验计划入口 |
| 2026-08-31 22:56 | /experiment-bridge | refine-logs/EXPERIMENT_TRACKER_20260831_225644.md | implementation | 初始实验跟踪表 |
| 2026-08-31 22:56 | /experiment-bridge | refine-logs/EXPERIMENT_TRACKER.md | implementation | 最新实验跟踪入口 |
| 2026-08-31 22:56 | /experiment-bridge | idea-stage/docs/research_contract.md | implementation | 结果主张与证据边界 |
| 2026-08-31 23:20 | /experiment-bridge | finetune/data/general-source.jsonl | implementation | 360 条通用分类来源数据 |
| 2026-08-31 23:20 | /experiment-bridge | finetune/data/general-train.jsonl | implementation | 按来源组隔离的训练集 |
| 2026-08-31 23:20 | /experiment-bridge | finetune/data/general-val.jsonl | implementation | 按来源组隔离的验证集 |
| 2026-08-31 23:20 | /experiment-bridge | finetune/data/general-test.jsonl | implementation | 锁定的独立测试集 |
| 2026-08-31 23:20 | /experiment-bridge | target/general-classifier-data-report.json | implementation | 数据分布与防泄漏报告 |
| 2026-08-31 23:20 | /run-experiment | target/general-classifier-sanity.json | implementation | MPS 健全性训练通过 |
| 2026-08-31 23:20 | /run-experiment | target/general-classifier-sanity.log | implementation | 健全性训练日志 |
| 2026-08-31 23:39 | /run-experiment | target/general-classifier-results.json | implementation | 基座与微调模型独立测试结果 |
| 2026-08-31 23:39 | /run-experiment | target/general-classifier-training.log | implementation | 完整 MPS 训练日志 |
| 2026-08-31 23:39 | /run-experiment | finetune/saves/qwen25-05b-general-cls/adapter | implementation | LoRA 适配器产物 |
| 2026-08-31 23:46 | /run-experiment | finetune/saves/qwen25-05b-general-cls-merged | implementation | 合并后的 16 位模型，未激活 |
| 2026-08-31 23:49 | /run-experiment | target/general-classifier-ollama.json | implementation | Ollama MLX 运行失败证据，不作为质量指标 |
| 2026-09-01 00:07 | /run-experiment | models/xling-general-cls-05b/xling-general-cls-05b-f16.gguf | implementation | llama.cpp 转换后的 16 位模型 |
| 2026-09-01 00:07 | /run-experiment | models/xling-general-cls-05b/xling-general-cls-05b-q4_k_m.gguf | implementation | 量化部署模型 |
| 2026-09-01 00:08 | /run-experiment | target/general-classifier-ollama.json | implementation | Q4 Ollama 独立测试结果 |
| 2026-09-01 00:14 | /experiment-audit | EXPERIMENT_AUDIT.md | implementation | 同系列新代理完整性审计，结论 WARN |
| 2026-09-01 00:14 | /experiment-audit | EXPERIMENT_AUDIT.json | implementation | 可机读完整性审计 |
| 2026-09-01 00:14 | /analyze-results | refine-logs/EXPERIMENT_RESULTS_20260901_001418.md | implementation | 训练与部署指标分析 |
| 2026-09-01 00:14 | /analyze-results | refine-logs/EXPERIMENT_RESULTS.md | implementation | 最新实验结果 |
