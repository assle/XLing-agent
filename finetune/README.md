# 通用心理支持路由分类器

当前实验使用 Qwen2.5-0.5B-Instruct，通过 LoRA（低秩适配，只训练少量参数的微调方法）学习“正常、焦虑、低落、高风险”四类标签。分类结果只用于消息分流和安全信号，不作诊断。

## 数据

```bash
.venv/bin/python finetune/scripts/generate_general_classifier_data.py
```

生成 360 条确定性模板数据：训练 200、验证 80、独立测试 80。四类标签平衡，覆盖十类生活场景，同一来源模板不会跨集合。自动检查会在出现来源组交叉、精确重复或跨集合近重复时失败。

数据当前尚未完成人工复核，因此所有指标均属于合成代理评测。

## 训练

先运行不读取独立测试集的健全性训练：

```bash
PYTORCH_ENABLE_MPS_FALLBACK=1 TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 \
  .venv/bin/python finetune/scripts/train_general_classifier.py \
  --sanity --local-files-only
```

通过后运行完整训练：

```bash
PYTORCH_ENABLE_MPS_FALLBACK=1 TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 \
  .venv/bin/python finetune/scripts/train_general_classifier.py \
  --local-files-only
```

训练先使用验证集选择最佳适配器，锁定后才读取独立测试集。结果写入 `target/general-classifier-results.json`。

## 当前结果

| 路径 | 准确率 | 宏平均 F1 | 高风险召回 | 输出有效率 |
|---|---:|---:|---:|---:|
| 未微调基座，16 位 | 72.50% | 65.55% | 5.00% | 100% |
| 微调后，16 位 | 97.50% | 97.50% | 100% | 100% |
| Q4_K_M + Ollama | 93.75% | 93.77% | 100% | 100% |

完整分析见 `refine-logs/EXPERIMENT_RESULTS.md`，完整性审计见根目录 `EXPERIMENT_AUDIT.md`。

## 包装

合并模型：

```bash
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 \
  .venv/bin/python finetune/scripts/package_general_classifier.py \
  --local-files-only
```

本次已经生成：

- LoRA 适配器：`finetune/saves/qwen25-05b-general-cls/adapter`
- 合并模型：`finetune/saves/qwen25-05b-general-cls-merged`
- 量化模型：`models/xling-general-cls-05b/xling-general-cls-05b-q4_k_m.gguf`
- Ollama 模型：`xling-general-cls-05b:latest`

模型文件体积较大，默认不提交到 Git。完成人工复核前，应用仍保留原默认分类器，不自动切换。
