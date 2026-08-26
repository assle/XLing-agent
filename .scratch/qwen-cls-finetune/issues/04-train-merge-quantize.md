# 04 - LoRA 训练、合并、量化

**What to build:** 在单张消费/专业卡上对 Qwen2.5-3B-Instruct 做 LoRA 微调，合并 LoRA 补丁回基座并 Q4_K_M 量化为 gguf，产出可被 Ollama 加载的本地分类器模型。

**Blocked by:** 01 - 数据准备与质量检查（需要训练集）

**Status:** ready-for-agent

- [ ] LLaMA-Factory 训练配置完整（rank 16、lr 1e-4、epoch 4、cutoff 256、bf16）
- [ ] 训练在单张消费/专业卡上可完成，不超显存
- [ ] LoRA 补丁合并回基座导出完整权重
- [ ] 完整权重经 Q4_K_M 量化为 gguf，体积在 2GB 量级
- [ ] gguf 经 Ollama 加载成功，手动输入几条学生文本能输出正确的情绪标签
- [ ] 训练配置与命令可从仓库复现
