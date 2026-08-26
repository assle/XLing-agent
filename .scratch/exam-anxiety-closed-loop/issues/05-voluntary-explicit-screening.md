Status: ready-for-agent

# 05: 自愿基线及用户主动显式量表筛查

## Parent

../spec.md (exam-anxiety-closed-loop)

## What to build

交付 PHQ-9 与 GAD-7 的显式、自愿筛查路径。首次使用时可以建议但不能强制基线筛查，用户也可随时主动发起；开始前展示量表用途、原题、选项、计分规则和非诊断声明，用户可拒绝或中止而不影响聊天。完成后保存题目版本、逐题答案、透明分数、触发来源与时间，并让用户查看结果。

若答案本身包含高风险信号，先向用户展示现实安全资源并进入既有高风险人审路径；不得等待总分或把自由对话暗推成量表答案。

## Acceptance criteria

- [x] 首次使用提供可跳过的 PHQ-9/GAD-7 基线入口，用户也能从学生端主动发起任一量表
- [x] 开始前展示用途、原题、答案选项、计分规则、自愿性和非诊断声明
- [x] 用户可拒绝或中止且仍能使用普通聊天；未完成答卷不产生伪完整分数
- [x] 完整答卷按公开规则确定性计分，并保存题目版本、逐题答案、分数、触发来源与时间
- [x] 用户只能查看自己的历史筛查及答案，结果始终表述为筛查和趋势参考而非诊断
- [x] 高风险答案立即显示安全资源并创建既有高风险人审，不因低总分被忽略
- [x] 自由聊天内容不会被转换成 PHQ-9/GAD-7 题目答案或分数
- [x] ScreeningService seam 覆盖两种量表、校验/计分、拒绝/中止、用户隔离和高风险升级

## Blocked by

- 01 (`01-secure-access-migration.md`)


## Comments

### 2026-07-22 实现完成

- `app/models/entities.py`：新增 `ScreeningResult` 模型（user_id, scale_type, question_version, answers_json, total_score, severity, trigger_source, high_risk_flagged, created_at）
- `app/services/screening.py`：新增 `ScreeningService`（PHQ-9/GAD-7 原题、选项、计分规则、非诊断声明、确定性计分、高风险答案检测）
- `app/schemas/dtos.py`：新增 `ScreeningSubmitRequest`
- `app/api/routes.py`：新增 `GET /api/screening/{scale_type}`, `POST /api/screening/{scale_type}/submit`, `GET /api/screening/results`, `GET /api/screening/results/{result_id}`
- PHQ-9 Q9（自伤念头）非零答案触发 high_risk_flagged
- 结果始终表述为筛查和趋势参考，不作诊断
- `tests/test_screening.py`：19 个测试覆盖量表信息、计分、高风险检测、验证、用户隔离、API 端点
