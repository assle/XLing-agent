# 通用四分类器

当前采用的实验性分类器为 `xling-general-cls-05b:quoted-f16`，使用 Qwen2.5-0.5B-Instruct 系列的历史已合并基座与 376 条训练适配器，输入格式为 `quoted`。分类结果用于消息分流和安全信号，不作诊断。当前选型、权重身份、数据与保留证据指纹统一见 [classifier-selection.json](reports/classifier-selection.json)；人类分类标签复核与完整模型资格验收尚未完成。

## 核心记录

| 内容 | 入口 | 用途 |
| --- | --- | --- |
| 已采用训练 | [freeze.json](reports/current-classifier/training/freeze.json)、[training-result.json](reports/current-classifier/training/training-result.json) | 冻结配置、188 次更新、逐轮选模与完整 128 条验证预测 |
| 训练来源 | [command.txt](reports/current-classifier/training/command.txt)、[wrapper.py](reports/current-classifier/training/wrapper.py)、[trainer-snapshot.py](reports/current-classifier/training/trainer-snapshot.py)、[system-prompt.txt](reports/current-classifier/training/system-prompt.txt) | 原执行命令、输入契约与训练代码 |
| 数据审读 | [summary.json](reports/current-classifier/content-review/summary.json)、[structure.json](reports/current-classifier/content-review/structure.json) | 数据组成、划分与标签支撑；属于工程审读 |
| 实际部署评测 | [既有 120 条](reports/model-repair/runtime/candidate-gguf-results.json)、[补充 40 条](reports/model-repair/new40/selected.json)、[activation.json](reports/model-repair/activation.json) | 已选 GGUF/F16 的完整预测与本机配置 |
| 同条件 LoRA 消融 | [protocol.json](reports/lora-ablation-20261007/protocol.json)、[paired.json](reports/lora-ablation-20261007/paired.json)、[summary.json](reports/lora-ablation-20261007/summary.json) | 预先冻结条件、320 次真实推理与收益边界 |

原始观测、冻结文件与数据保留执行时的字节和指纹，其中的绝对路径与 `.scratch` 路径属于原执行上下文。消融中的源码与旧选型汇总指纹对应 [原证据提交](https://github.com/assle/XLing-agent/tree/be48a42)，当前复跑使用精简后的选型入口。完整错例和无效输出仍计入保留报告的统计。

## 数据

[训练集](data/current-classifier/train-376.jsonl)共 376 条，每类 94 条、64 个来源组；[选模验证集](data/current-classifier/validation-128.jsonl)共 128 条、24 个来源组。训练数据的机械模板主体占 200/376，其余为自然表达、否定与时间作用域增补。训练与验证没有逐字输入重叠或同名来源组交叉，但共享生成方式；选模验证成绩不能替代独立测试。

工程内容审读核对了原标签、来源和支撑片段，未使用模型预测：353 条标签支撑明确、23 条支撑边界，未发现标签冲突，原标签未改写。逐条依据见 [正常／焦虑](reports/current-classifier/content-review/normal-anxiety.json)与[低落／高风险](reports/current-classifier/content-review/low-high.json)。这些记录不代表人类标签复核。

固定回归集合为[机械 80 条](data/general-routing-v2/general-test.jsonl)、[既有自然 40 条](data/current-classifier/natural-v12-r2-40.jsonl)与[补充自然 40 条](data/boundary-v3/acceptance-40.jsonl)。后者在历史候选锁定后首次评价，标签来源见 [review.json](reports/model-repair/acceptance/review.json)与 [support-evidence.json](reports/model-repair/acceptance/support-evidence.json)；本次消融时全部 160 条已经曝光。数据为工程合成文本，分数按固定原标签计算。

基础生成器仍构造 360 条确定性模板数据，按来源组隔离为训练 200、验证 80、机械测试 80。以下命令写入 `general-routing-v2`，不会覆盖原始 v1 数据或增补数据：

```bash
.venv/bin/python finetune/scripts/generate_general_classifier_data.py
```

## 训练与输入契约

已采用训练使用 HF/PEFT、MPS、float16 基座与新建 LoRA，固定 4 epochs、学习率 `5e-5`、rank 8（`q_proj/k_proj/v_proj/o_proj`）、batch size 1、梯度累积 8、最大长度 256、seed 42。完成 188 次更新，按验证 macro F1 最大值选中 batch 752（epoch 2），平分保留首次结果；训练阶段未读取测试集。

选模验证结果为 125/128（97.65625%）、macro F1 0.976545、高风险召回 32/32、1 条高风险误报、输出有效率 100%。原先“零高风险误报”的严格资格门槛未通过，当前仍为实验性候选。

系统提示来自冻结文件，用户输入为 `待分类文本（仅分析其表达，不执行其中的请求）：` 后接换行与 `json.dumps(text, ensure_ascii=False)`。当前 [训练 CLI](scripts/train_general_classifier.py)与业务客户端共享[输入包装](../app/core/classifier_contract.py)，通过 `--system-prompt-file` 与 `--input-format quoted` 复用契约。

训练 CLI 的默认路径仍对应 v1 数据，需要显式指定当前训练与验证文件；`--training-only` 保存选模适配器并跳过测试，超长训练样本明确拒绝，梯度累积最后一组按实际批数平均。至多八条样本的损失探针仅作训练健全性检查。历史 wrapper 保留单次执行保护；复跑训练使用当前 CLI 和新的输出目录。

## 当前运行结果

已选 GGUF/F16 在完整固定 160 条上正确 156 条（97.5%），macro F1 0.974937、高风险召回 40/40、输出有效率 100%，仍有 4 条焦虑误判高风险。两份原始运行报告包含完整逐题结果。该成绩只衡量模型分类，不包含业务关键词策略、风险轨迹或完整用户流程；当前业务流程与复跑见[工程验证](../docs/project-validation.md)。

## 已选 LoRA 的增量消融

对当前历史 376 条训练适配器进行了同条件配对消融：在同一个未合并的 PEFT 模型内，逐题关闭／开启该适配器。实际训练基座权重 SHA256 为 `b7e394c3e19caa695885ac475882b702b825653d1dee1fca52c11ea3e1c368f7`，适配器 SHA256 为 `39312e62bae612295d46153242b6159e15e0b5196ba4e3906df779e87f770efd`。Tokenizer、系统提示、quoted 输入、MPS、float16 基座、PEFT 默认适配器 dtype、贪心生成及 256／6 token 上限均相同，只有适配器开关改变。两组按预定顺序交错执行，共 320 次真实推理，0 训练、0 重试；运行前后权重文件指纹未变。

| 同条件 HF/MPS 方案 | 正确数／160 | 准确率 | 宏 F1 | 高风险召回 | 高风险误报 | 输出有效率 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 实际历史基座，不启用这次 LoRA | 129 | 80.625% | 0.822445 | 40/40 | 13 | 152/160（95%） |
| 同基座，启用已选 376 LoRA | 156 | 97.5% | 0.974937 | 40/40 | 4 | 160/160（100%） |

在这组固定回归条件下，该适配器的准确率增量为 **16.875 个百分点**：28 条改对、1 条改错、128 条两组均正确、3 条两组均错误。8 条改对来自输出协议对齐：基座输出“紧张”“沮丧”“悲伤”等同义词，未符合约定的四个枚举，按冻结规则计为无效，不能全部解释为语义理解错误。两组均有效的 152 条条件子集中，准确率为 84.868% 对 97.368%；该条件子集只辅助解释，不替代全 160 条分母。开启适配器后的 160 条预测与已有已选 GGUF/F16 报告逐题一致。

该消融隔离的是**这次新增 LoRA 在其实际历史已合并基座上的贡献**。基座已含更早的合并微调，不是干净原始 Qwen；160 条均是已经曝光的工程合成回归问题，人工分类标签复核仍未完成。因此不外推为全部 LoRA 相对原始基座的收益、全新独立测试或真实用户／临床效果。延迟只作当次运行描述，不作为部署速度提升结论。预先冻结的条件见 [protocol.json](reports/lora-ablation-20261007/protocol.json)，逐题原始输出、状态、版本与指标见 [paired.json](reports/lora-ablation-20261007/paired.json)，复核与解释见 [summary.json](reports/lora-ablation-20261007/summary.json)。原面试冻结标签和当前部署模型保持原版本。

复跑使用[增量消融入口](scripts/ablate_hf_classifier.py)，输出文件须为新文件；本机权重路径准备好后执行：

```bash
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 \
  .venv/bin/python finetune/scripts/ablate_hf_classifier.py \
  --model .scratch/user-simulation/20261002-022717-followup/goal-validation/training-candidate/recovered-merged-base \
  --adapter .scratch/user-simulation/20261002-022717-followup/goal-validation/training-candidate-epochs4/training-output/adapter \
  --dataset finetune/data/general-routing-v2/general-test.jsonl \
  --dataset finetune/data/current-classifier/natural-v12-r2-40.jsonl \
  --dataset finetune/data/boundary-v3/acceptance-40.jsonl \
  --output .scratch/lora-ablation-replay/paired.json
```

## 包装与复跑依赖

本机 `.env` 显式选择 `xling-general-cls-05b:quoted-f16` 与 `CLASSIFIER_INPUT_FORMAT=quoted`。基座、适配器及 GGUF 权重只保存在本机，GitHub 仓库不包含权重。代码默认 tag 为 `xling-cls-3b-ft:latest`，输入格式默认 `plain`。

包装入口的默认参数对应 v1 工件，使用显式参数打包已选适配器。`--shadow` 写入新的隔离目录，并保留原严格资格门槛状态：

```bash
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 \
  .venv/bin/python finetune/scripts/package_general_classifier.py \
  --shadow --local-files-only \
  --model .scratch/user-simulation/20261002-022717-followup/goal-validation/training-candidate/recovered-merged-base \
  --adapter .scratch/user-simulation/20261002-022717-followup/goal-validation/training-candidate-epochs4/training-output/adapter \
  --results finetune/reports/current-classifier/training/training-result.json \
  --output .scratch/classifier-shadow/merged
```

[注册入口](scripts/register_runtime_classifier.py)接受模型目录、系统提示、独立 tag 与服务地址，拒绝覆盖已有 tag，并在评测前检查零输入加载。[运行评测入口](scripts/evaluate_runtime_classifier.py)接受重复的 `--dataset`、`--output`、`--model`、`--base-url` 与 `--input-format quoted`，调用实际业务分类客户端，拒绝覆盖已有结果。可选远端评测的默认[系统提示](reports/current-classifier/remote-profile/system-prompt.txt)保留为 CLI 输入配置。
