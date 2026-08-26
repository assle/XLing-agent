Status: ready-for-agent

# PRD: 前端适配 -- 考研焦虑支持闭环切片 UI

> 本 PRD 固化 2026-07-22 `/grill-me` 已锁定的 9 项前端设计决策，遵守 ADR-0006 至 ADR-0009 与 `CONTEXT.md` 的领域词汇。

## Problem Statement

Xling 后端已完成考研焦虑支持闭环切片的全部 18 个 issue（rag-enhancement 02-04 + exam-anxiety-closed-loop 01-14），包括 bcrypt + JWT 登录、用户画像、CBT 结构化追问、24h 行动计划、次日 check-in、风险轨迹、量表筛查、记忆卡片、人审扩展、隐私说明和数据删除等。Agent runtime 已接通 CBT、行动计划和风险轨迹。

但前端目前只有 login 适配了 JWT，其余新功能均无界面入口。学生无法设置备考阶段、看不到 CBT 追问进度、无法查看和操作行动计划、无法管理记忆卡片、无法做量表筛查、无法访问隐私说明或删除数据。管理员无法看到扩展的人审上下文（接管原因、脱敏摘要、四种审核决定）。完整闭环（进入 -> 风险分流 -> CBT 追问 -> 行动计划 -> 次日 check-in -> 升级或完成）在 UI 层面不可达。

## Solution

将纯 HTML/CSS/JS 单页应用扩展为支持完整闭环切片的前端体验。代码架构从单文件 `app.js` 迁移到 ES Modules（浏览器原生 import/export，无构建工具），每个功能一个独立 `.js` 模块。新增 `privacy.html` 作为公开隐私说明页。

学生端新增：侧边栏用户画像面板（备考阶段选择）、聊天区 CBT 进度标签、聊天区下方可折叠行动计划面板（含 check-in）、侧边栏记忆卡片按钮（弹窗 CRUD）+ 量表筛查按钮（弹窗问卷）、新会话无记忆模式复选框 + 头部标签、设置区数据删除入口。后端 SSE 流扩展 `cbt` 和 `action_plan` 事件类型，让前端实时获取 CBT 进度和行动计划信息。

管理端扩展人审列表：接管原因 badge、脱敏摘要折叠区、四种审核决定按钮（放行/拒绝/转介/持续关注）+ 备注框。

## User Stories

### 隐私说明与数据删除

1. As a visitor, I want to read a plain-language privacy notice without logging in, so that I know what data is collected and how it is used before I sign up.
2. As a logged-in student, I want to access a data deletion entry from the student view, so that I can request deletion of all my data.
3. As a student, I want to see a complete impact list and irreversibility warning before confirming deletion, so that I make an informed decision.
4. As a student, I want my account and token immediately invalidated after deletion, so that my data cannot be accessed afterward.

### 用户画像与备考阶段

5. As a returning student, I want to see my exam prep stage displayed in the sidebar after login, so that I know the system is aware of my preparation context.
6. As a new student, I want to be prompted to set my exam stage with a plain-language explanation of why it is useful, so that I can make an informed choice.
7. As a student, I want to skip the profile setup and still use chat, so that I am not forced to share information.
8. As a student, I want to edit my exam stage, target exam, and exam date later, so that changes in my preparation plan stay accurate.
9. As a student, I want to clear optional profile fields, so that I can remove information I no longer want to share.

### CBT 结构化追问呈现

10. As a student in a consult conversation, I want CBT questions to appear as natural chat messages, so that the exchange feels like a conversation rather than a form.
11. As a student, I want a lightweight progress indicator showing I am in a structured support flow and how many dimensions are covered, so that I understand the process.
12. As a student, I want the progress indicator to show I can exit at any time, so that structured support does not feel coercive.
13. As a student, I want the progress indicator to disappear when CBT is complete, so that the interface returns to normal chat.

### 24 小时行动计划与 check-in

14. As a student who just completed CBT, I want to see the generated action plan in a persistent panel below the chat, so that I can reference it without scrolling through chat history.
15. As a student, I want to mark action plan items as complete one by one, so that I can track my progress.
16. As a student, I want to replace an action plan item that does not suit me, so that the plan stays relevant.
17. As a student returning the next day, I want to see a check-in prompt in the action plan panel, so that I can report how things went.
18. As a student, I want to choose improved/unchanged/worsened with optional notes during check-in, so that the system knows my status.
19. As a student, I want the action plan panel to collapse when no plan is active, so that it does not take up space.

### 记忆卡片与无记忆会话

20. As a student, I want to open a memory cards modal from the sidebar, so that I can see all cards the system remembers about me.
21. As a student, I want to create, edit, and delete memory cards, so that I control what the system remembers.
22. As a student, I want to confirm or reject system-suggested cards, so that only cards I approve become long-term memory.
23. As a student, I want to check a "no-memory mode" box when starting a new session, so that the conversation is not saved to long-term memory.
24. As a student, I want a persistent badge in the chat header during no-memory sessions, so that the privacy mode is unambiguous.
25. As a student, I want no-memory mode to leave existing cards unchanged, so that one private session does not erase prior choices.

### 量表筛查

26. As a student, I want to start a PHQ-9 or GAD-7 screening from the sidebar, so that I can self-assess when I choose to.
27. As a student, I want to see the purpose, questions, answer options, scoring rules, and non-diagnostic disclaimer before starting, so that my consent is informed.
28. As a student, I want to cancel or stop a screening at any time without losing chat access, so that participation remains voluntary.
29. As a student, I want to see my score and severity after completing the screening, so that the result is transparent.
30. As a student, I want to view my historical screening results, so that I can track trends.

### 管理端扩展人审

31. As an administrator, I want to see the handoff reason (HIGH_RISK_KEYWORD / RISK_TRAJECTORY_RISING / SUSTAINED_NO_IMPROVEMENT / USER_REQUEST / TIMEOUT) on each review item, so that I understand why the case was escalated.
32. As an administrator, I want to read a desensitized summary on each review item, so that I have context without seeing raw sensitive content.
33. As an administrator, I want to choose from four decisions (approve / reject / refer / monitor) with an optional note, so that I can record a nuanced review outcome.
34. As an administrator, I want already-decided reviews to be read-only, so that decisions cannot be silently overwritten.

## Implementation Decisions

### 代码架构

- 从单文件 `app.js` 迁移到 ES Modules。`<script type="module" src="/app.js">` 引入，浏览器原生支持 import/export，不需要构建工具。
- `app.js` 保留共享 state 对象和 API helper（`api()` 函数 + Bearer token 管理），各功能模块 import 需要的部分。
- 模块拆分：`app.js`（共享 state + API + 初始化）、`auth.js`（登录/重置/退出）、`profile.js`（用户画像面板）、`chat.js`（聊天 + CBT 进度标签 + 无记忆会话）、`screening.js`（量表筛查弹窗）、`action-plan.js`（行动计划 + check-in 面板）、`memory-cards.js`（记忆卡片弹窗）、`admin.js`（管理后台 + 扩展人审）。
- `index.html` 的 `<script>` 标签改为 `<script type="module" src="/app.js">`，各模块通过 import 链自动加载。

### 隐私说明页

- 新增 `privacy.html` 独立静态页面，无需登录即可访问。调用 `GET /api/privacy` 获取说明文本并渲染。底部有"返回应用"链接指向 `/`。
- 共享 `styles.css` 保持视觉一致。

### 数据删除入口

- 放在学生端设置区（侧边栏画像面板下方或头像菜单内）。展示完整影响清单 + 不可逆警告 + 确认按钮。确认后调用 `DELETE /api/account`，成功后清除本地 token 并跳转到登录页。

### 用户画像面板

- 登录表单隐藏后，原位置显示画像面板。首次进入（无画像数据）展示字段用途说明 + "填写" / "跳过" 按钮。已设置后显示紧凑状态（"冲刺阶段 · 考研 · 2026-12-21"）+ "编辑" 按钮。编辑时就地展开下拉选择（基础/强化/冲刺/考前/考后）+ 目标考试输入框 + 考试日期选择器。调用 `GET /api/profile/exam` 和 `PUT /api/profile/exam`。

### CBT 进度标签

- 聊天头部下方显示轻量标签："结构化支持 · {completed}/4 · 可随时退出"。数据来自 SSE `cbt` 事件。CBT 完成或用户退出后标签消失。

### SSE 事件扩展

- `ChatService.stream_chat` 在 `meta` 事件后、`token` 事件前新增 `cbt` 事件（字段：`active`、`completedCount`、`nextDimension`、`complete`）和 `action_plan` 事件（字段：`planId`、`items` 数组含 `id`/`content`/`order`）。
- 数据从 `AgentRunResult.steps` 中提取 CBT agent 步骤信息。改动集中在 `ChatService`，不涉及 runtime 逻辑。

### 行动计划面板

- 聊天区下方新增可折叠面板。有活动计划时展开显示条目列表，每个条目带复选框（标记完成）和"替换"按钮（弹出输入框输入新内容）。标题显示进度（"2/3 已完成"）。check-in 按钮在面板内出现，点击后展开改善/不变/恶化选择 + 备注输入框。无计划时面板隐藏。
- 调用 `GET /api/action-plans`、`POST /api/action-plans/items/{id}/complete`、`PUT /api/action-plans/items/{id}`、`GET /api/check-ins/pending`、`POST /api/check-ins`。

### 记忆卡片弹窗

- 侧边栏新增"记忆卡片"按钮，点击弹出模态框。模态框内展示卡片列表（已确认 + 待确认），每张卡片有编辑和删除按钮。待确认卡片有"确认"按钮。底部有"新建卡片"输入框。调用 `GET/POST/PUT/DELETE /api/memory-cards` 和 `POST /api/memory-cards/{id}/confirm`。

### 无记忆会话

- "新会话"按钮旁新增"无记忆模式"复选框。勾选后创建的会话请求携带 `noMemory: true`。会话期间在聊天头部显示"无记忆模式"标签。需要后端 `POST /api/chat/stream` 接受 `noMemory` 字段并在创建/获取会话时设置 `ChatSession.no_memory`。

### 量表筛查弹窗

- 侧边栏新增"量表筛查"按钮，点击弹出模态框。模态框内先选量表（PHQ-9 / GAD-7），然后展示说明页（用途、计分规则、非诊断声明 + "开始" / "取消"），再展示所有题目（单页，每题 4 个单选选项），提交后显示分数和结果。历史记录可通过模态框内 tab 查看。调用 `GET /api/screening/{scale_type}`、`POST /api/screening/{scale_type}/submit`、`GET /api/screening/results`。

### 管理端扩展人审

- 现有审核列表中每个 item 新增：接管原因 badge（颜色区分 5 种原因）、脱敏摘要折叠区（默认折叠，点击展开）。审核操作从 2 个按钮（approve/reject）改为 4 个按钮（approve/reject/refer/monitor），点击后出现备注输入框 + 确认按钮。已完成审核显示决定结果，不可再次操作。调用现有 `GET /api/admin/reviews` 端点（已包含新字段）。

## Testing Decisions

### SSE 事件扩展（后端）

- 扩展 `tests/test_tracer_bullet.py` 或新增测试，验证 CBT 流程中 `ChatService.stream_chat` 正确发出 `cbt` 和 `action_plan` 事件。只测试外部行为（事件是否发出、字段是否正确），不断言 ChatService 内部实现细节。
- Prior art：`tests/test_tracer_bullet.py` 的 `_run_message` + asyncio 模式。

### API 契约（后端）

- 已有 22 个测试文件覆盖全部新 API 端点。不需要新增后端 API 测试。

### 前端验证（Playwright via node_repl MCP）

- 使用 Playwright 自动化浏览器验证各功能流程。启动 dev server 后，通过 `node_repl` MCP 执行 Playwright 脚本。
- 验证内容：登录流程、画像设置、CBT 聊天 + 进度标签、行动计划面板操作、check-in 提交、记忆卡片 CRUD、无记忆会话切换、量表筛查完整流程、隐私页访问、数据删除、管理端扩展人审。
- 每个流程验证：页面元素存在、交互响应正确、状态更新符合预期。关键步骤截图。
- Prior art：无现有前端测试基础设施，此为首次引入 Playwright 验证。

## Out of Scope

- 前端响应式设计优化（移动端适配）-- 当前优先桌面端功能完整性。
- 前端单元测试框架引入（Jest/Vitest 等）-- Playwright 端到端验证已足够。
- 国际化（i18n）-- 当前仅中文。
- 无障碍（a11y）审计 -- 后续迭代。
- 前端构建工具（Vite/Webpack 等）-- ES Modules 原生加载不需要构建。
- CBT 追问在 LangGraph runtime 中的端到端 Playwright 验证 -- 仅验证 custom runtime 路径。

## Further Notes

- 实现优先级按闭环依赖排序：隐私页+数据删除 -> 用户画像 -> CBT+行动计划+check-in -> 记忆卡片 -> 量表筛查 -> 管理端扩展人审。
- 所有新 API 端点已在后端 issue 01-14 中实现并测试通过。前端只需调用现有 API。
- `noMemory` 字段需要后端 `POST /api/chat/stream` 和 `ChatRequest` DTO 做小幅改动以支持无记忆会话切换。
- 隐私页 `privacy.html` 需要在 Caddyfile 中确认公开可访问（当前 Caddyfile 已配置 `/api/privacy` 公开，静态文件通过 `/` 挂载自动可访问 `privacy.html`）。
