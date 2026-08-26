# 03 - 评估脚本与基座 zero-shot 基线

**What to build:** 写一个分类评测脚本，跑基座 Qwen2.5-3B-Instruct zero-shot（不微调，直接用提示词分类）在 240 条留出验证集上，产出"微调前"基线报告，作为后续 before/after 对比的基准。

**Blocked by:** 01 - 数据准备与质量检查（需要留出验证集）

**Status:** ready-for-agent

- [ ] 评测脚本借鉴现有风险评测骨架，自包含可独立运行
- [ ] 对 240 条留出集运行基座 Qwen2.5-3B-Instruct zero-shot 分类
- [ ] 报告含整体准确率、四类 precision/recall/F1、混淆矩阵、高风险 recall
- [ ] 报告输出到 target 目录，与现有 RAG 评测产出风格一致
- [ ] 基线数字明确记录为 before 基准，供后续 after 对比引用
