Status: ready-for-agent

# 01: bcrypt 强制重置 + 24h JWT 登录

## Parent

../spec.md (exam-anxiety-closed-loop)

## What to build

把现有 Basic Auth + SHA-256 登录端到端迁移为 bcrypt + Bearer JWT。现有 SHA-256 凭据只允许进入显式密码重置流程，不能继续访问受保护业务；完成重置或使用 bcrypt 凭据登录后，签发有效期固定为 24 小时的 access token。学生、管理员、SSE 对话与后台接口统一通过 token 鉴权，继续执行现有角色隔离。登录界面改为显式登录/重置体验，并移除演示密码预填、自动登录和公开默认密码提示。

迁移必须保留现有账号和角色，不依赖删除重建数据库；新建及种子账号从一开始使用 bcrypt。

## Acceptance criteria

- [x] 新密码和新建账号只保存 bcrypt 哈希，正确密码可验证，错误密码不可验证
- [x] 现有 SHA-256 凭据不能访问任何受保护业务，只能完成一次显式密码重置
- [x] 密码重置成功后旧凭据失效，新凭据可登录且数据库中的哈希已迁移为 bcrypt
- [x] 登录成功返回 Bearer access token，token 的有效期为 24 小时并包含用户身份与角色所需声明
- [x] 缺失、篡改或过期 token 被拒绝；Basic Auth 不再能访问受保护 API 或 SSE 对话
- [x] 学生不能进入管理员能力，管理员不能以管理员账号发起学生对话，现有角色语义保持不变
- [x] 前端不再预填/公开默认密码或自动登录，并能完成登录、强制重置、退出和过期后重新登录
- [x] schema 迁移对已有账号数据安全，测试无需清空数据库即可验证旧哈希迁移
- [x] Security seam 覆盖 bcrypt、强制重置、JWT 签发/篡改/过期、API 与 SSE 鉴权

## Blocked by

None - can start immediately


## Comments

### 2026-07-22 实现完成

- `app/core/security.py`：完全重写。bcrypt 替代 SHA-256（`hash_password` / `verify_password`），`is_legacy_hash` + `verify_legacy_password` 检测和处理旧 SHA-256 哈希，`create_access_token` / `decode_access_token` 签发和验证 24h JWT，`current_user` 从 Bearer token 提取用户
- `app/api/routes.py`：新增 `POST /api/auth/login`（bcrypt -> JWT；legacy -> resetRequired）和 `POST /api/auth/reset`（验证旧密码，迁移到 bcrypt，签发 JWT）
- `app/schemas/dtos.py`：新增 `LoginRequest` 和 `ResetPasswordRequest`
- `app/core/config.py`：新增 `bcrypt_rounds` / `jwt_secret_key` / `jwt_algorithm` / `jwt_access_token_expire_minutes`
- `app/core/bootstrap.py`：种子账号使用 bcrypt（`hash_password` 已切换到 bcrypt）
- `app/static/index.html` + `app/static/app.js`：移除预填凭据和密码提示，登录改为 POST /api/auth/login + Bearer token，新增密码重置表单，移除自动登录
- `tests/test_security.py`：22 个测试覆盖 bcrypt 哈希、旧哈希检测、JWT 签发/篡改/过期/错误密钥、登录/重置端点、Basic Auth 拒绝、角色隔离
