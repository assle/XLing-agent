Status: ready-for-agent

# 产品需求：备考焦虑支持闭环

> 本 PRD 固化 2026-07-22 `/grill-with-docs` 已锁定的 11 项决策，并遵守 ADR-0006 至 ADR-0009 与 `CONTEXT.md` 的领域词汇。

## Problem Statement

Xling 已能完成通用心理支持对话、单条风险评估、知识检索和高风险人工审核，但尚未形成对考研和考公学生真正有连续价值的**备考焦虑支持**产品：

1. 学生的备考阶段、目标考试和考试日期没有进入可管理的**备考画像**，回复与知识检索无法稳定感知基础、强化、冲刺、考前或考后阶段。
2. 咨询对话目前由语言模型一次性自由生成，不能保证覆盖触发事件、想法、身体反应和行为四个方面，也没有把对话落成可执行、可追踪的 24 小时行动。
3. 系统没有次日反馈，无法知道行动是否完成、焦虑是否缓解，也无法把“进入 -> 消息分流 -> 安全风险评估 -> 认知行为四维追问 -> 行动计划 -> 次日反馈 -> 安全升级或完成”串成一个**支持闭环**。
4. 长期上下文目前是隐式的消息摘要；用户无法查看、修改或删除系统记住的内容，也不能选择**无记忆会话**。
5. 风险判断主要看单条消息。连续恶化但单条未达 HIGH 的情况不会因会话内最近 3 条或跨会话最近 7 天的上升趋势而升级。
6. PHQ-9 和 GAD-7 尚未作为知情、自愿、可解释的**自愿量表筛查**存在，无法提供初次使用基线、风险触发建议或学生主动筛查。
7. 知识库仍以通用校园心理内容为主，分块没有**备考阶段**信息，无法按阶段过滤；人工审核记录也缺少标准化的**审核触发原因**、**脱敏摘要**和审核结论。
8. 当前 Basic Auth + SHA-256 密码、24 小时之外无明确生命周期的认证方式、手工 JSON 解析和无统一重试策略构成安全与可靠性硬伤。
9. 当前 Docker 编排只覆盖应用依赖，没有面向公网的自动 HTTPS、隐私说明页和用户数据删除入口，不能形成可交付演示环境。

## Solution

交付一个面向考研和考公学生的完整**支持闭环**。学生登录后自愿建立包含备考阶段、目标考试和考试日期的备考画像，可选择是否进行基线自愿量表筛查、是否启用长期记忆。发生焦虑时，系统先做消息分流，再结合当前信号和风险轨迹进行安全风险评估，然后围绕触发事件、想法、身体反应和行为逐步追问；信息完整后生成包含结构化条目的 24 小时行动计划。学生可逐条完成行动，并在次日反馈中报告执行与感受变化；系统据此完成本轮支持闭环，或因风险上升、持续无改善、学生请求等原因安全升级到人工审核。

长期记忆采用可管理的混合模式：少量结构化备考画像字段与学生可见、可改、可删的记忆卡片并存；无记忆会话不产生新的长期记忆。知识库扩充备考全周期内容，并以备考阶段信息过滤候选内容。PHQ-9 和 GAD-7 只在学生自愿时进行，题目、答案、计分方式和用途可见，结果仅用于筛查和趋势参考，不作诊断。

同时完成可交付所需的安全底座：密码改为 bcrypt，现有凭据强制重置；登录改为 24 小时访问令牌；语言模型产生的风险评估、认知行为四维追问状态和行动计划使用明确的数据结构约束，并进行有上限的重试。最终通过云服务器、容器编排、域名和自动加密连接完成部署，公开隐私说明页和数据删除入口。

## User Stories

### 账号、安全与知情

1. As a returning user, I want to be required to reset a legacy password before continuing, so that my account no longer relies on the old SHA-256 credential.
2. As a user, I want my new password stored with bcrypt, so that a database leak is harder to exploit.
3. As a user, I want a successful login to issue a 24-hour access token, so that I do not send my password with every request.
4. As a user, I want expired or invalid access tokens rejected consistently, so that protected data is not exposed.
5. As an administrator, I want role checks to continue after JWT migration, so that student and human-review capabilities remain separated.
6. As a user, I want to read a plain-language privacy notice before using sensitive features, so that I know what is collected, why, and for how long.
7. As a user, I want an accessible data-deletion entry, so that I can request deletion without finding an administrator offline.

### 备考画像与备考阶段

8. As a user, I want to set my target exam, exam date, and exam prep stage, so that support can reflect my actual preparation context.
9. As a user, I want to edit my user profile later, so that stage changes and exam-plan changes stay accurate.
10. As a user, I want the system to explain why profile fields are useful, so that I can make an informed choice about providing them.
11. As a user, I want to skip optional profile fields, so that I can still receive support without over-sharing.
12. As a user, I want my exam prep stage to influence stage-aware guidance, so that advice fits foundation, reinforcement, sprint, pre-exam, or post-exam needs.

### 长期记忆与隐私控制

13. As a user, I want to see all memory cards saved about me, so that long-term memory is transparent.
14. As a user, I want to create or approve a memory card, so that genuinely useful context can carry across sessions.
15. As a user, I want to edit a memory card, so that inaccurate or outdated context does not persist.
16. As a user, I want to delete a memory card, so that I control what the system remembers.
17. As a user, I want to start a no-memory session, so that sensitive conversation content is not added to long-term memory.
18. As a user, I want a visible indication when a session is in no-memory mode, so that its privacy behavior is unambiguous.
19. As a user, I want no-memory mode to leave existing memory cards unchanged, so that one private session does not erase prior choices.

### 自愿量表筛查

20. As a new user, I want to be offered an optional PHQ-9 or GAD-7 baseline, so that I may establish a self-reported reference point.
21. As a user, I want to start PHQ-9 or GAD-7 whenever I choose, so that screening is not limited to system triggers.
22. As a user, I want to see the original questions, answer choices, scoring rule, purpose, and non-diagnostic disclaimer, so that consent is informed.
23. As a user, I want to decline or stop a screening without losing access to chat, so that participation remains voluntary.
24. As a user, I want to review my answers and result, so that the score is explainable rather than inferred.
25. As a user, I want a rising risk trajectory to produce an optional screening suggestion, so that I can choose a structured check when it may help.
26. As a human reviewer, I want high-risk screening answers to trigger the same safety and review path as high-risk chat, so that risk is not trapped in a questionnaire.
27. As a user, I want screening results described as screening and trend reference only, so that the system does not imply a diagnosis.

### 认知行为四维追问

28. As a user expressing exam anxiety, I want the system to ask natural, context-aware follow-up questions, so that the exchange does not feel like a rigid form.
29. As a user, I want the conversation to cover the triggering event, thoughts, body reactions, and behavior, so that important parts of my experience are not skipped.
30. As a user, I want already-answered CBT dimensions recognized, so that I am not asked to repeat myself.
31. As a user, I want only the next missing dimension asked at a time, so that the conversation remains manageable.
32. As a user, I want to pause or leave the CBT flow, so that structured support does not become coercive.
33. As a developer, I want CBT progress represented as schema-validated state, so that completion can be verified deterministically.

### 24 小时行动计划与次日反馈

34. As a user, after the four CBT dimensions are complete, I want a practical 24-hour action plan, so that reflection becomes an immediate next step.
35. As a user, I want the plan split into structured action-plan items, so that each step is clear and independently completable.
36. As a user, I want each action-plan item to be small, safe, and relevant to my exam prep stage, so that the plan is realistic.
37. As a user, I want to mark action-plan items complete one by one, so that progress is visible.
38. As a user, I want to see active and completed action plans, so that I can resume after leaving the page.
39. As a user, I want to decline or replace an unsuitable action item, so that the plan remains collaborative.
40. As a user, I want next-day feedback tied to my action plan, so that the system can ask what happened rather than restarting from zero.
41. As a user, I want to report completion and whether anxiety improved, stayed the same, or worsened, so that the closed loop has an outcome.
42. As a user, I want completed or improved next-day feedback to close the current support loop, so that I have a clear sense of progress.
43. As a user, I want non-completion met with non-judgmental adjustment rather than blame, so that setbacks remain discussable.
44. As a user, I want worsening or sustained no improvement to trigger safer follow-up or human review, so that the system does not repeat ineffective advice indefinitely.

### 风险轨迹与人工审核

45. As a user, I want the system to notice worsening risk across my latest three in-session messages, so that gradual escalation is not missed.
46. As a user, I want the system to consider the latest seven days across sessions, so that repeated distress is not treated as unrelated events.
47. As an administrator, I want risk trajectory thresholds configurable, so that policy can be tuned without code changes.
48. As a human reviewer, I want rising trajectories to escalate even when no single message is HIGH, so that I can intervene earlier.
49. As a human reviewer, I want every review request to state a standardized handoff reason, so that I know why it entered the queue.
50. As a human reviewer, I want a desensitized summary containing the current difficulty, trajectory, CBT summary, and action-plan status, so that I can review useful context with less exposure.
51. As a human reviewer, I want to record approve, reject, refer, or watch decisions with a note and reviewer identity, so that review outcomes are auditable.
52. As a human reviewer, I want user-requested handoff and timeout represented alongside automated triggers, so that all review paths share one vocabulary.
53. As a user, I want review summaries sanitized before human handoff, so that unnecessary personal identifiers are removed.

### 备考阶段知识与交付

54. As a user, I want guidance covering the full exam-prep cycle, so that the system remains useful from foundation through post-exam rebuilding.
55. As a user, I want retrieved guidance filtered by my exam prep stage, so that irrelevant stages do not crowd out useful content.
56. As an administrator, I want knowledge chunks tagged with one or more exam prep stages, so that stage filtering is maintainable.
57. As a developer, I want a RAG evaluation dataset to prove exam-stage filtering, so that retrieval changes are regression-tested.
58. As a visitor, I want the public service available over HTTPS on a domain, so that credentials and sensitive conversation are encrypted in transit.
59. As an operator, I want the application, dependencies, and Caddy started through Docker, so that deployment is repeatable on a low-cost cloud server.
60. As an operator, I want Caddy to obtain and renew HTTPS certificates automatically, so that certificate maintenance is not manual.

## Implementation Decisions

- **场景边界**：本产品需求只交付备考焦虑支持的第一个支持闭环。所有学生可见命名、知识内容和流程文案使用“备考焦虑支持”“备考阶段”“支持闭环”等术语表词汇。
- **支持闭环状态**：一轮支持闭环至少区分进入、消息分流、安全风险评估、认知行为四维追问、行动计划、等待次日反馈、安全升级和完成状态。状态必须可跨请求恢复；同一学生可以保留历史支持闭环，但同一时刻只推进明确的活动支持闭环。
- **认知行为四维追问**：触发事件、想法、身体反应和行为是不可省略的四个方面。代码决定缺失方面与完成条件，语言模型只负责基于当前上下文生成自然措辞和提取结构化字段；不得以自由生成文本宣称流程已完成。
- **结构化输出**：风险评估、认知行为四个方面的提取与下一问、行动计划均使用明确的数据结构约束。解析错误以及可重试的模型或网络错误采用有上限、带退避的重试；重试耗尽后进入明确、安全的兜底路径，不把格式错误的内容当作有效业务状态。
- **行动计划模型**：使用 `ActionPlan` 与 `ActionPlanItem` 两级持久化模型。计划关联用户、会话/闭环、创建时间、24 小时目标窗口和状态；条目有稳定标识、顺序、内容、完成状态与完成时间，可逐条完成。替换条目保留可审计的计划状态，不静默改写已完成记录。
- **次日反馈**：次日反馈关联行动计划，记录条目执行情况、学生自报变化（改善、不变、恶化）和可选说明。产品提供“次日”入口，但不以精确 24 小时作为阻止学生操作的硬门槛；进入次日反馈时基于当前计划状态决定完成、调整或安全升级。
- **备考画像**：结构化字段至少包含备考阶段、目标考试和考试日期；字段对学生可见、可编辑，除产品正常运转所需的账号字段外不强制填写。备考阶段分为基础、强化、冲刺、考前和考后。
- **长期记忆**：结构化备考画像与记忆卡片共同构成跨会话背景。记忆卡片有稳定标识、正文、来源、创建时间和更新时间，必须提供查看、创建或确认、编辑和删除能力，不做不可见的隐式画像。
- **无记忆会话**：会话创建时保存是否启用长期记忆。关闭时，该会话内容不生成新记忆卡片、不自动更新备考画像，也不读取记忆卡片作为对话背景；风险与安全记录、必要的会话记录不因无记忆模式而关闭。界面必须明确说明这一区别。
- **风险轨迹**：每次可形成风险信号的消息、自愿量表筛查或次日反馈产生带时间的轨迹点。会话内窗口固定为最近 3 条，跨会话窗口固定为最近 7 天。安全升级阈值与“持续上升”的判定参数可以配置；轨迹可提高风险等级，但不得降低明确高风险信号的等级。
- **自愿量表筛查**：支持 PHQ-9 与 GAD-7。入口为首次使用时的可选基线、风险触发后的可选建议和学生主动发起；三种入口都必须得到主动同意。保存题目版本、逐题答案、透明计分结果、时间和触发来源。不得从自由对话反推量表答案或分数。
- **筛查安全边界**：量表结果只标为筛查和趋势参考，不给诊断。高风险答案先展示现实安全资源，并进入既有安全风险评估和人工审核路径；学生拒绝或中止不会限制普通对话。
- **知识阶段信息**：知识分块增加备考阶段信息，可表达单一阶段、多个阶段或全阶段。检索在有备考画像阶段时先按阶段过滤，再沿用现有检索方式；无阶段时允许全库候选。
- **知识内容**：扩充基础、强化、冲刺、考前、考后全周期内容，覆盖阶段性焦虑、作息、计划失衡、临场调节、失利与考后重建，并保持非诊断、安全转介边界。
- **人工审核扩展**：人工审核记录增加审核触发原因、脱敏摘要、审核决定、审核备注和审核人。审核触发原因包括高风险关键词、风险轨迹上升、持续无改善、学生请求和超时；审核决定至少支持放行、拒绝、转介和持续关注。
- **脱敏摘要**：提交人工审核前由现有隐私清洗能力处理，摘要只包含当前困境、风险轨迹、认知行为四维追问摘要和行动计划状态所需信息，不默认复制完整对话或量表答案。
- **认证迁移**：新密码只以 bcrypt 保存。检测到旧 SHA-256 凭据时，不允许继续以旧方案访问受保护业务，必须完成显式密码重置；种子账号也使用 bcrypt。登录成功返回 24 小时 JWT access token，后续 API 与 SSE 均使用 Bearer token，角色授权语义保持不变。
- **数据删除**：提供登录学生可达的数据删除入口。删除范围至少覆盖备考画像、记忆卡片、自愿量表筛查、行动计划、次日反馈、会话消息、风险报告与关联人工审核数据；执行前明确影响并要求确认。法律或安全原因必须保留的最小记录若存在，要在隐私说明中明确，而不是静默保留。
- **交付拓扑**：云服务器运行 Docker 编排的应用、MySQL、Redis 和 Caddy；域名 DNS 指向服务器，Caddy 作为唯一公网入口并自动签发/续期 HTTPS。数据库、Redis 和应用内部端口不直接暴露公网。隐私说明页可在未登录状态访问，删除入口在登录后可达。
- **现有能力复用**：复用当前异步智能体运行流程、人工审核的中断与恢复、人工审核队列、隐私清洗、流式输出、知识检索评测和学生与管理员双视图。新支持闭环必须在主路径完整工作；不得因实现新功能破坏普通聊天快速路径和已有高风险安全兜底。
- **接口语义**：学生侧提供登录和重置、备考画像、记忆卡片、会话模式、自愿量表筛查、行动计划、次日反馈和数据删除等资源接口；管理员侧提供风险轨迹配置与人工审核接口。具体地址和内部文件布局由实现决定，资源归属与角色权限必须在服务端校验。

## Testing Decisions

- **测试原则**：只验证外部行为和持久化结果，不断言 LangGraph 内部节点数、prompt 原文、ORM 私有实现或模型调用次数。结构化输出与 retry 测试可通过假 provider 注入成功、格式错误、暂时错误和永久错误序列，验证最终行为而非 tenacity 内部细节。
- **主要流程测试**：用结果固定的假语言模型驱动一条完整支持闭环：进入焦虑场景 -> 消息分流 -> 安全风险评估 -> 逐步补齐四个方面 -> 生成结构化行动计划 -> 完成部分或全部条目 -> 次日反馈 -> 完成或安全升级。另验证普通聊天快速路径、高风险直接人工审核、学生中止认知行为四维追问和无记忆会话。
- **Service seam — ScreeningService**：覆盖 PHQ-9/GAD-7 原题版本、答案校验、透明计分、三类触发来源、自愿拒绝/中止、非诊断展示和高风险答案升级。
- **Service seam — UserProfileService**：覆盖可选字段、备考阶段枚举、读取/修改、用户归属与向检索/闭环提供阶段上下文。
- **Service seam — MemoryCardService**：覆盖列出、创建/确认、编辑、删除、用户隔离，以及无记忆会话不读写记忆卡片。
- **Service seam — ActionPlanService**：覆盖 24 小时窗口、结构化条目顺序、逐条完成、替换、活动/历史计划与跨请求恢复。
- **风险轨迹服务测试**：覆盖会话内最近 3 条、跨会话最近 7 天、时间边界、阈值配置、持续上升时安全升级、明确高风险不降级，以及自愿量表筛查和次日反馈信号进入同一轨迹。
- **Security seam**：覆盖 bcrypt 新哈希与验证、旧 SHA-256 强制重置、JWT 签发/角色/篡改/24 小时过期、所有受保护 API 拒绝 Basic Auth、Structured Output schema 校验、tenacity 成功重试与耗尽后的安全 fallback。
- **RAG eval seam**：扩展现有 dataset 与 runner，以指定备考阶段运行检索；验证返回内容只来自匹配阶段或全阶段 metadata，并保留现有 recall@K、precision@K、MRR、NDCG@K、hit rate 汇总。不以 RRF/rerank 指标作为目标。
- **界面与接口集成**：对学生归属、管理员角色、登录和重置、关键资源增删改查、支持闭环事件、量表自愿入口、记忆控制、行动条目完成、人工审核决定、隐私页和删除确认做端到端行为验证。前端测试关注学生能否完成路径，不锁定页面内部结构或文案细节。
- **部署验证**：在干净环境构建并启动 Docker 编排，验证应用只经 Caddy 暴露、HTTP 跳转 HTTPS、证书配置可由域名环境变量驱动、健康检查正常、数据卷持久化、隐私说明页无需登录可读。
- **Prior art**：沿用现有 assessment/risk guardian 的确定性风险用例、LangGraph runtime/interrupt/resume 流程测试、ReviewService 的 SQLite service 测试，以及 RAG eval 的自包含 SQLite + dataset 模式。

## Out of Scope

- 通用校园心理健康的其他垂直场景，以及多模态、主动语音关怀、校级群体看板。
- 心理疾病诊断、治疗建议、用药建议，或用自由对话暗推 PHQ-9/GAD-7 分数。
- 纯固定问卷或完全由语言模型自由决定是否覆盖认知行为四个方面。
- 不可见、不可编辑的长期 embedding 画像；无记忆会话也不等于删除安全必需记录。
- 超过备考画像字段和记忆卡片范围的自动人格推断。
- RAG multi-query、BM25/向量 RRF、cross-encoder/LLM rerank、parent-document 等检索增强。
- 原生移动端、短信、邮件或推送式次日提醒；本支持闭环只提供应用内可恢复的次日反馈入口。
- 多租户机构配置、复杂 counselor 排班、外部医院转诊系统集成。
- refresh token、社交登录、多因素认证和完整账号找回体系；本切片只锁定 24 小时 access token 与旧密码强制重置。
- Kubernetes、自动伸缩、多区域容灾、完整 CI/CD 或付费域名采购自动化。
- 代表用户购买云服务器或域名；交付物是可在用户提供的服务器与域名上重复部署的配置和文档。

## Further Notes

- ADR-0006 决定以备考焦虑支持作为首个场景；ADR-0007 决定认知行为四个方面必须覆盖并使用自然措辞；ADR-0008 决定备考画像、记忆卡片和无记忆会话共同构成长效支持；ADR-0009 决定采用自愿量表筛查而非对话暗推。本产品需求不重开这些决策。
- 已确认的优先级是“安全硬伤 + 支持闭环”合并交付。任务应按可端到端验证的纵向切片拆分，使每个切片可单独领取、演示和验证，并让安全底座尽早解除后续切片的依赖。
- 当前数据库由应用启动时 `create_all` 建表，没有迁移框架。涉及生产数据的 schema 演进必须在实现 issue 中明确安全迁移/回滚方式，不能依赖删除重建数据库。
- 当前前端把演示账号密码预填并自动登录；JWT/bcrypt slice 完成时必须移除这一行为和公开默认密码提示。
- “次日”是产品语义而非严格调度保证：学生第二天回到应用时应直接看到待反馈的行动计划。本产品需求不包含外部通知渠道。
- 风险阈值可调不表示普通用户可见或可改；只有管理员/部署配置可调整。学生端继续避免展示后台风险标签。
- 数据删除属于高影响操作，实现 issue 必须采用显式确认、归属校验和可测试的事务边界；本 PRD 不授权在规划阶段删除任何现有数据。
