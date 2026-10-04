# Experiment Audit Report

**Date**: 2026-09-01
**Auditor**: GPT-5.6-Sol ultra（同系列新代理，只读，暂定结论）
**Project**: Xling

## Overall Verdict: WARN

## Integrity Status: warn

### A. Ground Truth Provenance: PASS

标签由确定性数据模板写入 `output` 字段，不来自被评模型输出。所有数据均标记为 `deterministic-template-v1` 和 `humanReviewStatus=pending`，因此只能作为合成代理真值。

### B. Score Normalization: PASS

准确率、精确率、召回率、F1 和混淆矩阵直接由逐条预测与数据标签计算；没有使用模型自身最大值、均值或输出范围进行归一化。

### C. Result File Existence: PASS

数据、日志、逐条预测和当前结果编号完全对齐。基座与微调后指标可独立精确复算，训练日志与结果文件一致。

### D. Dead Code Detection: PASS

当前版本没有未调用的指标函数，声明指标均出现在结果文件中。

### E. Scope Assessment: WARN

仅运行一个基座、一套配置、一个随机种子和一次完整训练。360 条数据全部由同一模板体系生成且未经人工复核，测试集实际包含 8 个模板来源组。

### F. Evaluation Type: synthetic_proxy

这是合成代理评测，不是真实用户数据、人工真值或临床评测。

## Action Items

- 建立独立来源、人工复核的边界测试集。
- 增加间接高风险、含糊表达、转述和类别交界案例。
- 运行多个随机种子并报告波动。
- 单独评测硬规则优先与分类器故障降级。

## Claim Impact

- “在当前合成模板数据上显著提升”：supported。
- “通用心理支持场景中更可靠”：needs qualifier。
- “可直接生产或临床部署”：unsupported。
