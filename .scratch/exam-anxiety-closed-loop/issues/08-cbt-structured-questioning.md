Status: ready-for-agent

# 08: 四维 CBT 结构化追问

## Parent

../spec.md (exam-anxiety-closed-loop)

## What to build

在考研焦虑支持路径中交付可跨请求恢复的 CBT 结构化追问。代码以触发事件、想法、身体反应、行为四个维度作为完成硬约束；Structured Output 从用户当前回答中提取已覆盖维度并选择下一缺失维度，LLM 只生成结合用户画像与上下文的自然问法。每次只追问一个必要维度，已回答内容不重复询问，用户可以暂停或退出。

学生端以正常对话呈现追问，并能看到当前处于结构化支持流程而非诊断问卷。四维齐全时产生明确的“可制定行动计划”状态，供下一 slice 消费。

## Acceptance criteria

- [x] 考研焦虑输入可进入 CBT 结构化追问，普通 CHAT 快速路径不受影响
- [x] 流程状态分别记录触发事件、想法、身体反应、行为，只有四维齐全才标记完成
- [x] 单条回答覆盖多个维度时一次性识别，后续不会重复询问已完成维度
- [x] 每轮最多提出一个下一缺失维度，措辞结合备考阶段和已有上下文且不泄露内部标签
- [x] Structured Output 不合法时按统一重试/fallback 处理，不把错误提取写入完成状态
- [x] 用户可暂停或退出；再次进入同一活动闭环时能恢复已完成维度
- [x] 前端清晰表示这是可退出的支持对话，不将其呈现为诊断或强制问卷
- [x] Agent runtime 流程测试覆盖单维、多维、重复信息、四维完成、暂停恢复和 CHAT 不进入

## Blocked by

- 02 (`02-structured-risk-output-retry.md`)
- 03 (`03-user-profile-exam-stage.md`)


## Comments

### 2026-07-22 实现完成

- `app/services/cbt.py`：新增 `CBTService` 和 `CBTState`
  - 4 维度硬约束：触发事件、想法、身体反应、行为
  - `CBTExtractionSchema` Pydantic schema 从用户回答中提取已覆盖维度
  - LLM 提取 + 关键词 heuristic fallback
  - 已覆盖维度不重复询问，每轮最多一个缺失维度
  - 支持暂停/恢复（paused 标志），状态可序列化
  - 四维齐全时 is_complete=True，产生"可制定行动计划"状态
- `tests/test_cbt.py`：19 个测试覆盖状态属性、heuristic 提取、LLM 提取、多维度识别、不覆盖、下一问生成、序列化、顺序流程完成
