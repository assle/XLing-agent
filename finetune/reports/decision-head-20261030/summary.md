# 决策头分类器实验：单次前向架构对比（2026-10-30）

## 背景与架构

借鉴 Strands Decider / Cloudflare Clef 的决策模型范式（调研见 `docs/research/2026-10-30-decision-classifier-alternatives.md`），在已选生成式分类器的**同一基座、同一训练数据、同一训练超参**下，训练一个不做自回归生成的分类器：

- 冻结基座（`recovered-merged-base`，float16，MPS）+ 新建 LoRA（rank 8，q/k/v/o_proj）+ 固定标签线性决策头（hidden 896 → 4）
- 输入契约与线上一致：冻结系统提示 + `quoted` 用户包装 + 聊天模板，取最后一个真实 token 位置的隐藏表示，决策头单次前向输出四类 logits，softmax 得概率
- 标签集合固定为本任务四类，是"选项打分"架构在固定标签场景的最简形态；不包含请求时定义标签的泛化能力
- 训练在 HF float16 权重上进行（GGUF 量化权重不可训练）；自定义决策头不能进入 GGUF/Ollama，本实验用 HF Transformers 本地推理评测，**未改动任何生产运行时配置**

## 训练与选模

- 数据：train-376（每类 94）、validation-128；未读取任何固定回归测试集
- 超参与已选生成式训练完全对齐：4 epochs、lr 5e-5（决策头 1e-3）、batch 1、梯度累积 8、max_length 256、seed 42，共 188 次更新
- 按验证 macro F1 最大值选模（平分取先）：**最佳验证 macro F1 0.9922**（已选生成式为 0.9765），高风险召回 1.0
- 训练结果与逐轮验证历史：`training-result.json`；权重：`finetune/saves/qwen25-05b-decision-head-376/`（adapter + head.pt）

## 固定 160 例回归集对比

完整逐例结果：`evaluation-160.json`；已选生成式结果见 `../classifier-selection.json`。

| 指标 | 已选生成式（GGUF/F16，Ollama） | 决策头（HF/float16，MPS） |
| --- | --- | --- |
| 正确数 / 160 | 156 | **160** |
| accuracy | 0.975 | **1.0** |
| macro F1 | 0.9749 | **1.0** |
| 高风险召回 | 40/40 | 40/40 |
| 高风险误报 | 4 | **0** |
| 输出有效率 | 100% | 100%（架构上无无效输出） |
| P50 延迟 | 约 150 ms | **33.7 ms** |
| P95 延迟 | 约 175–222 ms | **41.0 ms** |

分数据集：机械 80 条、自然 40 条、补充自然 40 条均全对。

历史 4 条"焦虑误判高风险"样本（classifier-selection.json remainingIssues）在决策头下全部改对：

| 样本 | 预测 | 置信度 | P(高风险) |
| --- | --- | --- | --- |
| 872b3071fb391510 | 焦虑（正确） | 0.9316 | 0.068 |
| 0c47121395108533 | 焦虑（正确） | 0.9983 | 0.002 |
| 73c2179f9f94b48f | 焦虑（正确） | 0.9073 | 0.093 |
| 230f2f6922ee6231 | 焦虑（正确） | **0.5471** | 0.453 |

## 概率输出的价值

全 160 例的最低置信度（0.5471）恰好出现在历史边界样本 230f2f6922ee6231 上：该样本被判对，但高风险概率达 0.453。生成式方案无法暴露这种不确定性；决策头的概率分布支持"低置信度回退 / 高风险概率阈值"等分层安全策略，这是架构替换在精度与速度之外的第三个收益点。

## 限制（不外推）

1. 160 例均为已曝光的工程合成回归集，与训练数据共享生成方式；满分**不代表**自然表达独立泛化、真实用户或临床效果。人类分类标签复核仍未完成。
2. 延迟对比跨推理栈（Ollama GGUF vs HF/MPS float16），只作当次描述；两者同机但不同栈，不作为部署速度结论。
3. 本实验未包含业务关键词策略、风险轨迹与完整用户流程；生产分类路径未改动。
4. 决策头与 LoRA 权重仅保存本机，未注册任何 Ollama tag。

## 复跑

```bash
# 训练（training-only，不读测试集）
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 .venv/bin/python finetune/scripts/train_decision_head_classifier.py \
  --model .scratch/user-simulation/20261002-022717-followup/goal-validation/training-candidate/recovered-merged-base \
  --system-prompt-file finetune/reports/current-classifier/training/system-prompt.txt \
  --input-format quoted --local-files-only --training-only \
  --output-dir finetune/saves/qwen25-05b-decision-head-376 \
  --result finetune/reports/decision-head-20261030/training-result.json

# 160 例评测（结果文件必须为新文件）
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 .venv/bin/python finetune/scripts/evaluate_decision_head_classifier.py \
  --model .scratch/user-simulation/20261002-022717-followup/goal-validation/training-candidate/recovered-merged-base \
  --adapter finetune/saves/qwen25-05b-decision-head-376/adapter \
  --head finetune/saves/qwen25-05b-decision-head-376/head.pt \
  --dataset finetune/data/general-routing-v2/general-test.jsonl \
  --dataset finetune/data/current-classifier/natural-v12-r2-40.jsonl \
  --dataset finetune/data/boundary-v3/acceptance-40.jsonl \
  --output finetune/reports/decision-head-20261030/evaluation-160.json \
  --system-prompt-file finetune/reports/current-classifier/training/system-prompt.txt \
  --input-format quoted --local-files-only
```

## 后续可选方向（未实施，另行决策）

- 新鲜盲测集上的独立验证，再讨论是否替换生产分类器
- 部署形态：HF/ONNX 独立推理服务（自定义头无法进入 GGUF/Ollama）
- 利用概率输出的高风险阈值与低置信度回退策略
- 若未来需要请求时定义标签，再评估 Strands 式 pointer head 与通用决策数据训练
