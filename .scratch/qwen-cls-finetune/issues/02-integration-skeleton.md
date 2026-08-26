# 02 - 业务接入骨架与单元测试（假分类器）

**What to build:** 不依赖 GPU 训练，用假分类器（FakeAiClient 返回中文标签词）端到端打通"分类器替换风险评估第二层"的接入架构，验证情绪标签到完整评估的映射、三层降级行为和既有安全 override 不被破坏。这是让后续真模型接入变容易的 prefactor slice。

**Blocked by:** None - can start immediately

**Status:** ready-for-agent

- [ ] AiClient 支持分类器专用模型名和一个简化的分类提示词（只要求输出情绪标签词，不复用要求 5 字段 JSON 的现有心理评估提示词）
- [ ] 新增 `ollama_classifier_model` 配置项，默认指向分类器模型名，与对话模型名分开
- [ ] 心理评估服务在非关键词路径上调用分类器，将返回的中文标签映射为 EmotionLabel，并用规则补齐情绪分数、风险等级、置信度、摘要
- [ ] 第一层高风险关键词短路原样生效（命中即 HIGH 且不调用分类器）
- [ ] 第三层保守兜底原样生效（分类器调用失败时降级为保守 MEDIUM）
- [ ] 现有通用大模型 5 字段 JSON 评估路径保留为代码内备用，不删除
- [ ] 分类器本地专属，不随 AI_PROVIDER 切换
- [ ] 单元测试覆盖：四类标签分别映射到正确的 emotion/risk/emotion_score；高风险关键词命中仍短路 HIGH；分类器失败走保守兜底
- [ ] intent==RISK 强制 HIGH 的 override 仍生效（复用现有 risk guardian 测试验证）
