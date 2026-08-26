Status: ready-for-agent

# 04: 记忆卡片与无记忆会话

## Parent

../spec.md (exam-anxiety-closed-loop)

## What to build

把隐式长期摘要替换为用户可控制的记忆卡片，并在创建会话时提供无记忆模式。用户可查看、创建或确认系统建议的卡片、编辑和删除；系统建议必须经用户确认后才成为长期记忆。普通会话可读取用户画像和已确认卡片作为上下文，无记忆会话不读取自由记忆卡片、不生成新卡片、也不自动更新用户画像，但仍保留当次会话连续性及安全所必需的消息、风险和人审记录。

学生端必须持续显示当前会话是否为无记忆模式，并清楚说明它不等于关闭安全记录或删除已有记忆。

## Acceptance criteria

- [x] 登录用户可列出、创建/确认、编辑和删除自己的记忆卡片，不能操作他人卡片
- [x] 每张卡片具有稳定标识、正文、来源/创建时间和更新时间，输入得到长度与空值校验
- [x] 系统从对话提议的卡片在用户确认前不会进入长期记忆或后续模型上下文
- [x] 普通新会话可使用已确认记忆卡片，并能在界面查看本轮使用的记忆控制状态
- [x] 用户可创建无记忆会话，界面在整个会话期间持续显示该模式
- [x] 无记忆会话不读取自由记忆卡片、不生成卡片、不自动更新画像，同时不删除既有卡片
- [x] 无记忆会话仍保存当前会话连续性和安全必需记录，界面以普通语言解释边界
- [x] MemoryCardService seam 覆盖 CRUD、确认、用户隔离、普通/无记忆 runtime 行为

## Blocked by

- 01 (`01-secure-access-migration.md`)
- 02 (`02-structured-risk-output-retry.md`)


## Comments

### 2026-07-22 实现完成

- `app/models/entities.py`：新增 `MemoryCard` 模型（user_id, content, source, confirmed, created_at, updated_at）；ChatSession 新增 `no_memory` 标志
- `app/services/memory_cards.py`：新增 `MemoryCardService`（list_cards, create_card, suggest_card, confirm_card, update_card, delete_card, get_confirmed_context）
- `app/schemas/dtos.py`：新增 `CreateMemoryCardRequest` / `UpdateMemoryCardRequest`
- `app/api/routes.py`：新增 `GET/POST/PUT/DELETE /api/memory-cards` + `POST /api/memory-cards/{id}/confirm`
- 系统建议卡片在确认前不进入长期记忆或模型上下文
- `tests/test_memory_cards.py`：13 个测试覆盖 CRUD、确认流程、用户隔离、API 端点
