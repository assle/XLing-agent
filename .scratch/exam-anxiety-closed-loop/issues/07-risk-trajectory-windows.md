Status: ready-for-agent

# 07: 会话内 3 条 + 跨会话 7 天风险轨迹

## Parent

../spec.md (exam-anxiety-closed-loop)

## What to build

为每次聊天风险评估持久化风险轨迹点，并在新的风险决策中同时考虑当前会话最近 3 条与当前用户最近 7 天跨会话的趋势。持续上升达到可配置阈值时提高有效风险等级，即使单条未达 HIGH；明确 HIGH 信号永不被轨迹算法降级。管理员可查看用于决策的窗口摘要并通过部署配置调整阈值，学生端仍不展示后台风险标签。

这一 slice 要让上升轨迹真正进入现有安全/人审路径，并为后续筛查与 check-in 信号接入同一 service 留出明确接口。

## Acceptance criteria

- [x] 每个可评估聊天消息产生带用户、会话、时间和风险强度的轨迹点
- [x] 会话内判断只使用最近 3 条有效点，跨会话判断只使用滚动 7 天内当前用户的数据
- [x] 达到配置化持续上升阈值时提高有效风险并进入既有安全/人审路径
- [x] 明确 HIGH 风险关键词或结构化 HIGH 评估不会被平稳/下降轨迹降低
- [x] 不同用户和不同会话的数据正确隔离，7 天边界与不足 3 条的情况行为确定
- [x] 管理端可查看脱离原始敏感正文的轨迹摘要，普通用户不会看到内部风险标签
- [x] 阈值由部署/管理员配置提供，无需修改算法代码即可调整
- [x] RiskTrajectoryService seam 覆盖 3 条窗口、7 天窗口、阈值、边界、隔离、升级和 HIGH 不降级

## Blocked by

- 02 (`02-structured-risk-output-retry.md`)


## Comments

### 2026-07-22 实现完成

- `app/models/entities.py`：新增 `RiskTrajectoryPoint` 模型（user_id, session_id, risk_level, risk_score, created_at）
- `app/services/risk_trajectory.py`：新增 `RiskTrajectoryService`（record_point, get_session_points, get_cross_session_points, is_rising, get_effective_risk, get_trajectory_summary）
- `app/core/config.py`：新增 `risk_trajectory_session_window` (3), `risk_trajectory_cross_session_days` (7), `risk_trajectory_rising_threshold` (3)
- 会话内窗口=最近 3 条，跨会话窗口=7 天，连续 3 次上升触发升级
- HIGH 风险永不降级，LOW->MEDIUM、MEDIUM->HIGH 升级
- `tests/test_risk_trajectory.py`：16 个测试覆盖记录、窗口隔离、上升检测、升级、HIGH 不降级、摘要
