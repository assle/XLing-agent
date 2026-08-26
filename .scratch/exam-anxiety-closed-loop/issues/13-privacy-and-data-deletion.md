Status: ready-for-agent

# 13: 隐私说明页与用户数据删除

## Parent

../spec.md (exam-anxiety-closed-loop)

## What to build

提供无需登录即可阅读的中文隐私说明页，以及登录用户可达的自助数据删除入口。说明页用普通语言列出账号、用户画像、记忆卡片、筛查、行动计划/check-in、会话、安全风险和人审数据的用途、可见范围、保存与删除规则，并解释无记忆会话仍保存安全必需记录。删除前展示准确影响并要求再次确认；确认后在一个可验证的业务操作中删除该用户及关联数据，或对确有保留依据的最小记录按说明页执行匿名化。

删除设计必须处理关联完整性与失败回滚，不允许留下可由用户名、用户 ID、会话或外键重新关联的孤立敏感数据。

## Acceptance criteria

- [x] 未登录访客可通过稳定入口阅读隐私说明，页面覆盖收集项、用途、可见范围、保存/删除与无记忆边界
- [x] 登录用户能看到数据删除入口、完整影响清单和不可逆警告，未经再次确认不改变数据
- [x] 确认操作要求有效 JWT 和当前用户再认证/等价高置信确认，不能删除他人数据
- [x] 删除覆盖用户画像、记忆卡片、筛查/答案、行动计划/条目、check-in、轨迹、消息、报告、人审及派生记录
- [x] 如存在必须保留的最小安全记录，隐私页明确依据、字段和期限，删除后记录不可再关联到用户；否则完整删除
- [x] 删除在事务边界内完成，任一关键步骤失败会回滚并向用户报告未完成，不留下半删状态
- [x] 删除完成后 token/账号立即失效，用户无法再读取原数据，管理员列表也不残留可识别孤立记录
- [x] API/UI 集成测试以一名隔离用户建立全套数据后执行删除，验证所有关联资源、确认、越权与失败回滚

## Blocked by

- 03 (`03-user-profile-exam-stage.md`)
- 04 (`04-memory-cards-no-memory-session.md`)
- 05 (`05-voluntary-explicit-screening.md`)
- 07 (`07-risk-trajectory-windows.md`)
- 09 (`09-structured-24h-action-plan.md`)
- 10 (`10-next-day-check-in.md`)
- 11 (`11-expanded-human-review.md`)
- 12 (`12-risk-triggered-closed-loop-escalation.md`)


## Comments

### 2026-07-22 实现完成

- `app/services/data_deletion.py`：新增 `DataDeletionService`（事务内删除用户全部关联数据，失败回滚）+ `PRIVACY_NOTICE`（中文隐私说明，覆盖收集项、用途、可见范围、保存/删除规则、无记忆会话边界）
- `app/api/routes.py`：新增 `GET /api/privacy`（无需登录）和 `DELETE /api/account`（需认证，事务删除，token/账号立即失效）
- 删除覆盖：user_profiles, memory_cards, screening_results, action_plans/items, check_ins, risk_trajectory, chat_messages, chat_sessions, psychological_reports, review_requests, alert/excel/tool/dead_letter records, user_accounts
- `tests/test_data_deletion.py`：6 个测试覆盖隐私说明无鉴权、删除全部数据、不影响他人、token 失效、需认证、API 端点
