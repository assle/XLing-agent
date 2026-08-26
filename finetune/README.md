# Qwen 情绪分类器微调

基于 Qwen2.5-3B-Instruct，用 LoRA（低秩适配，只训练少量参数的微调方法）在 2400 条校园心理情绪分类数据上微调出一个只输出情绪标签的分类器，替换风险评估的第二层。

## 前置条件

- 一张消费/专业显卡（如 RTX 4090 24G），已安装 CUDA
- Python 3.10+，已安装 LLaMA-Factory：`pip install llamafactory`
- 已克隆 llama.cpp（用于量化）：`git clone https://github.com/ggerganov/llama.cpp`
- 已安装 Ollama（用于部署）

## 完整流程（7 步）

### 1. 数据准备

把 2400 条合成数据切成训练集（2160）和验证集（240，每类 60）：

```bash
python finetune/scripts/split_data.py
```

产物在 `finetune/data/`：`train.jsonl`、`val.jsonl`、`dataset_info.json`。

按 `finetune/data/quality-checklist.md` 人工抽检约 50 条，重点查焦虑↔低落边界和高风险误标。

### 2. 基座 zero-shot 基线（微调前）

用基座模型不微调直接分类，产出"微调前"数字：

```bash
# Ollama 已拉取 qwen2.5:3b
python -m app.cls_eval.runner --provider ollama --model qwen2.5:3b
```

报告输出到 `target/cls-eval-report.json`。记下 accuracy、macroF1、高风险 recall 作为 before 基准。

### 3. LoRA 训练

```bash
llamafactory-cli train finetune/configs/train_lora.yaml
```

单卡约 1-2 小时。训练配置：rank 16、lr 1e-4、epoch 4、cutoff 256、bf16。产物（LoRA 补丁）在 `finetune/saves/qwen25-3b-cls/`。

### 4. 合并 LoRA

把训练出的补丁合并回基座，导出完整权重：

```bash
llamafactory-cli export finetune/configs/merge_lora.yaml
```

产物在 `finetune/saves/qwen25-3b-cls-merged/`。

### 5. 量化

把完整权重量化成 Q4_K_M gguf（约 2GB）：

```bash
LLAMA_CPP_DIR=/path/to/llama.cpp ./finetune/scripts/quantize.sh
```

产物在 `models/xling-cls-3b-ft/xling-cls-3b-ft-q4_k_m.gguf`。

### 6. Ollama 部署

```bash
./scripts/create-classifier-model.sh
```

创建 Ollama 模型 `xling-cls-3b-ft:latest`，和对话模型 `xling-qwen2.5-7b-ft:latest` 并存。

### 7. 评估对比（微调后）

用同一个评估脚本跑微调后模型：

```bash
python -m app.cls_eval.runner --provider ollama --model xling-cls-3b-ft:latest
```

对比步骤 2 的 before 数字，得到整体 F1 和高风险 recall 的提升。

另可用现有风险评测跑整个 assess 链路（含关键词短路 + 分类器 + 兜底），在 50 个真实 case 上对比：

```bash
python -m app.risk_eval.runner --provider ollama
```

## 配置说明

- 分类器模型名：`xling-cls-3b-ft:latest`（可在 `.env` 用 `OLLAMA_CLASSIFIER_MODEL` 覆盖）
- 分类器本地专属，不随 `AI_PROVIDER` 切换；Ollama 不可用时降级到保守兜底
- 对话模型仍用 `xling-qwen2.5-7b-ft:latest` 或云端 API，两者独立

## 目录结构

```
finetune/
├── configs/
│   ├── train_lora.yaml      # LLaMA-Factory 训练配置
│   └── merge_lora.yaml      # LoRA 合并配置
├── data/
│   ├── train.jsonl           # 训练集 2160 条
│   ├── val.jsonl             # 验证集 240 条
│   ├── dataset_info.json     # LLaMA-Factory 数据集注册
│   └── quality-checklist.md  # 人工抽检清单
└── scripts/
    ├── split_data.py         # 数据切分（可复现）
    └── quantize.sh           # gguf 量化

models/xling-cls-3b-ft/
└── Modelfile                 # Ollama 分类器模型定义

app/cls_eval/
└── runner.py                 # 分类器评估脚本
```
