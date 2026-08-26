Status: ready-for-agent

# 03: 用户画像与备考阶段上下文

## Parent

../spec.md (exam-anxiety-closed-loop)

## What to build

交付用户可自助管理的结构化用户画像，包含备考阶段、目标考试与考试日期。首次进入时解释字段用途并允许跳过，之后可随时查看和编辑。备考阶段严格使用基础、强化、冲刺、考前、考后五个领域值，并作为 agent runtime 与后续阶段感知检索的显式上下文，不从自由对话暗中推断或覆盖。

这一 slice 包含安全的 schema 演进、归属校验的用户 API、学生端画像界面和 service 测试；不要求用户填满可选字段才能聊天。

## Acceptance criteria

- [x] 登录用户可读取和更新自己的备考阶段、目标考试与考试日期，不能读取或修改他人画像
- [x] 备考阶段只接受基础、强化、冲刺、考前、考后，日期与字段长度得到校验
- [x] 首次进入解释字段用途并允许全部跳过；未填写画像的用户仍可正常聊天
- [x] 用户可在后续修改或清空可选字段，读取结果与持久化状态一致
- [x] agent runtime 能获得当前画像的显式阶段上下文，但不会从对话静默改写画像
- [x] 学生端展示当前画像及编辑入口，不向普通用户暴露后台风险字段
- [x] 对已有用户安全增加画像数据，不依赖删除重建数据库
- [x] UserProfileService seam 覆盖枚举、可选字段、更新、清空、用户隔离与 runtime 上下文

## Blocked by

- 01 (`01-secure-access-migration.md`)


## Comments

### 2026-07-22 实现完成

- `app/core/enums.py`：新增 `ExamStage` 枚举（基础/强化/冲刺/考前/考后）
- `app/models/entities.py`：新增 `UserProfile` 模型（user_id, exam_stage, target_exam, exam_date）
- `app/services/user_profile.py`：新增 `UserProfileService`（get_profile, update_profile, get_stage_context, 枚举验证, 可选字段清空, 用户隔离）
- `app/schemas/dtos.py`：新增 `UpdateUserProfileRequest` / `UserProfileResponse`
- `app/api/routes.py`：新增 `GET /api/profile/exam` / `PUT /api/profile/exam` 端点
- `tests/test_user_profile.py`：16 个测试覆盖 CRUD、枚举验证、清空、用户隔离、API 端点、stage context
