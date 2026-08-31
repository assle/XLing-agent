# 实验跟踪

| Run | 阶段 | 状态 | 产物 | 备注 |
|---|---|---|---|---|
| M0 | 数据生成与防泄漏 | done | `target/general-classifier-data-report.json` | 360 条；无来源组交叉、精确重复或跨集合近重复 |
| M1 | 健全性训练 | done | `target/general-classifier-sanity.json` | 固定样本损失 0.2743 → 0.1210，验证准确率 87.5% |
| M2 | 基座独立测试 | done | `target/general-classifier-results.json` | 80 条；准确率 72.5%，宏平均 F1 65.55% |
| M3 | LoRA 训练 | done | `finetune/saves/qwen25-05b-general-cls/adapter` | 50 次参数更新；验证集选择最佳检查点 |
| M4 | 微调后独立测试 | done | `target/general-classifier-results.json` | 准确率 97.5%，宏平均 F1 97.50%，高风险召回 100% |
| M5 | 替换门槛与审计 | done | `refine-logs/EXPERIMENT_RESULTS.md` | 自动门槛通过；审计 WARN；模型已包装但因人工复核未完成而未激活 |
