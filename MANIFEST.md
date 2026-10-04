# Research Output Manifest

登记仍保留的历史研究产物；训练日志和模型文件等本地产物不随源码提交。当前分类器数据、实验报告和运行边界统一见 [finetune/README.md](finetune/README.md)。

| Timestamp | Skill | File | Stage | Description |
|-----------|-------|------|-------|-------------|
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
| 2026-09-01 00:07 | /run-experiment | models/xling-general-cls-05b/xling-general-cls-05b-f16.gguf | implementation | llama.cpp 转换后的 16 位模型 |
| 2026-09-01 00:07 | /run-experiment | models/xling-general-cls-05b/xling-general-cls-05b-q4_k_m.gguf | implementation | 量化部署模型 |
| 2026-09-01 00:08 | /run-experiment | target/general-classifier-ollama.json | implementation | Q4 Ollama 独立测试结果 |
| 2026-09-01 00:14 | /experiment-audit | EXPERIMENT_AUDIT.md | implementation | 同系列新代理完整性审计，结论 WARN |
| 2026-09-01 00:14 | /experiment-audit | EXPERIMENT_AUDIT.json | implementation | 可机读完整性审计 |
