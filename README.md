# Xling

Xling 是一个面向备考学生的心理支持与日常陪伴项目，也是用于展示人工智能应用、后端系统设计和算法评测能力的面试项目。它提供支持性沟通、校园知识检索、行动计划和高风险人工审核，但不进行医学诊断，也不能替代心理咨询师、医生或紧急救援服务。

## 面试展示重点

- 人工智能应用可复现：每份评测记录模型、分类器、提示词、嵌入模型、重排模型、索引、数据、校准产物和代码版本。
- 安全链路有硬边界：明确高风险规则优先于模型判断和知识检索，高风险流程必须进入人工审核。
- 算法结论可验证：提供 40 条端到端案例、100 条中文检索难例、风险概率校准和共形预测实验；未达门槛的 BGE 与校准结果不会被包装成上线成功。
- 后端状态可恢复：一轮支持过程使用单事务保存，工具任务支持唯一约束、原子抢占和租约恢复，人工审核检查点可以跨服务重启恢复。
- 失败路径可解释：Redis、向量服务、检查点和模型不可用时均有明确回退或安全降级策略。

## 核心能力

- 学生端 SSE（服务器主动推送事件）流式聊天，前端可展示打字机式输出。
- 会话历史回溯：侧边栏"历史会话"面板可查看、切换回任意旧对话；切换新会话时自动清空行动计划面板。
- 行动计划与回复同步：认知行为四维追问完成后生成的 24 小时行动计划条目会注入聊天回复的系统提示，确保回复内容与计划条目一致。
- 使用 bcrypt 保存密码，并通过 JWT（带签名的登录令牌）实现学生与管理员角色隔离。
- LangGraph 多智能体工作流：Memory、Supervisor、Knowledge、RiskGuardian、Companion、Counselor；未安装 LangGraph 时自动回退到自研有限循环运行时。
- 消息分流：先区分日常对话、心理支持和明确风险；日常问题不查知识库，后两类才进入知识检索和安全风险评估。
- Chroma 向量 RAG（检索增强生成，即先查资料再回答）知识库：支持 Markdown、txt、PDF 文件上传和自动切块，同时保留本地混合检索兜底。
- 可选 BGE-M3 中文检索和专用重排模型；只有质量、安全和延迟同时达标时才允许替换默认检索。
- 安全风险评估：高风险词典优先、分类器评估和保守兜底；可选温度缩放与共形预测表达不确定性。
- 后台报告：记录情绪标签、情绪分数、风险等级、置信度和摘要，但学生端不展示后台评估结果。
- 数据闭环：咨询或风险消息完整写入 MySQL，短期上下文写入 Redis，高风险消息通过持久化任务队列写入 Excel 台账并发送邮件预警。
- 人工审核恢复：高风险中断状态默认持久化到异步 SQLite，服务重启后仍可批准或拒绝；状态丢失或损坏时发送固定安全回复。
- 事务一致性：会话变化、学生消息、心理报告、人工审核请求和工具任务一次提交，任一步骤失败都会一起回滚。
- 本地微调模型接入：支持通过 Ollama 加载 `xling-qwen2.5-7b-ft-q4_k_m.gguf`。
- OpenAI-compatible API 接入：也可切换到云端模型。
- MCP（模型上下文协议）工具服务：暴露 Excel 报告写入和风险通知工具；关闭持久化工具队列时可改用 MCP 调用。
- 评测闭环：覆盖端到端、知识检索、安全风险、回复质量和分类器，并统一记录产物版本。

## 技术栈

```text
语言：Python
Web 框架：FastAPI
服务运行：Uvicorn / ASGI
数据库：MySQL，SQLAlchemy ORM，PyMySQL 驱动
短期记忆：Redis
配置管理：pydantic-settings，.env
AI 接入：Ollama，本地微调 GGUF 模型，OpenAI-compatible API，Mock Provider
Agent 编排：LangGraph，多 Agent 图工作流，自研 runtime 兜底
RAG：本地知识库切块、OpenAI Embeddings、Chroma、BGE-M3、专用重排、本地混合检索兜底
流式输出：Server-Sent Events
检查点：LangGraph AsyncSqliteSaver，纯数据状态，30 天保留期限
评测：端到端、检索、安全风险、概率校准、回复质量、分类器
文档解析：pypdf
Excel 台账：openpyxl
邮件预警：SMTP / smtplib
前端：原生 HTML / CSS / JavaScript
认证：bcrypt + JWT Bearer Token
工具协议：MCP
```

说明：当前 Python 版已经提供 LangGraph runtime，入口在 `app/agents/langgraph_runtime.py`；同时保留 `app/agents/runtime.py` 作为无框架兜底。RAG 默认使用 Chroma 本地持久化向量库；未安装 Chroma、未配置 `OPENAI_API_KEY` 或向量服务异常时，会自动回退到本地 `hybrid_score` 检索，避免演示环境中断。

## 目录结构

```text
app/
├── agents/          # LangGraph 多智能体编排、纯数据检查点和自研运行时兜底
├── api/             # 账号、学生支持、管理端和系统状态路由
├── core/            # 配置、数据库、安全、版本清单和启动初始化
├── knowledge/       # 内置校园心理知识库
├── mcp_tools/       # MCP 工具服务
├── models/          # SQLAlchemy 实体
├── schemas/         # Pydantic DTO
├── services/        # AI、聊天、检索、风险校准、事务、报告和工具服务
└── static/          # 原生前端页面

evals/
├── e2e/             # 40 条生产聊天闭环评测
├── rag/             # 100 条中文检索难例与多方案对照
├── risk/            # 风险基线、概率校准和共形预测
├── quality/         # 回复质量评测
└── classifier/      # 分类器评测

docs/adr/             # 当前有效的关键架构决策

models/xling-qwen2.5-7b-ft/
├── Modelfile        # Ollama 模型定义
└── README.md        # GGUF 模型放置说明

scripts/
├── run-dev.sh
├── start-ollama.sh
├── create-finetuned-model.sh
└── package-release.sh
```

## Agent loop

每轮对话进入一个有边界的多智能体工作流，防止心理安全场景出现无限自主循环：

```text
MemoryAgent
-> SupervisorAgent
-> CHAT -> CompanionAgent -> 流式输出
-> CONSULT/RISK -> RiskGuardianAgent -> risk_guardian_gate
                    -> HIGH -> 人工审核
                    -> LOW/MEDIUM -> KnowledgeAgent -> CBT/CounselorAgent
```

各 Agent 分工：

- `MemoryAgent`：优先从 Redis 读取本会话短期记忆；Redis 为空时从 MySQL 最近消息回填，并生成本轮记忆摘要。
- `SupervisorAgent`：判断 `CHAT / CONSULT / RISK`，决定是否进入心理支持链路。
- `KnowledgeAgent`：将学生输入改写为知识库查询词，执行 RAG 检索。
- `RiskGuardianAgent`：执行后台安全风险评估，同时保留高风险词库硬兜底。
- `CompanionAgent`：处理普通学习、编程、校园事务和闲聊。
- `CounselorAgent`：结合记忆、RAG 和风险评估，生成心理支持回复 prompt。

## 安装依赖

```bash
pip install -r requirements.txt
```

`requirements.txt` 已包含：

```text
langchain-core
langgraph
chromadb
pymysql
redis
```

如果交付环境暂时无法安装 LangGraph，系统仍会自动回退到自研 runtime，不影响 Mock 演示和基本功能。

开发和测试使用单独的依赖清单，避免把测试工具装进生产镜像：

```bash
pip install -r requirements-dev.txt
python -m pytest
```

BGE-M3 中文检索与专用重排使用独立依赖，默认生产安装不加载约 4GB 的模型运行栈：

```bash
pip install -r requirements-bge.txt
```

## 质量验证

```bash
python -m pytest -q
ruff check app evals tests
mypy --ignore-missing-imports app evals
```

当前完整测试结果为 436 项通过。另有 4 项真实 MySQL 事务故障注入测试，只有显式提供名称以 `_test` 结尾的隔离数据库时才运行：

```bash
MYSQL_TEST_DATABASE_URL='mysql+pymysql://root:root@127.0.0.1:3306/xling_issue8_test?charset=utf8mb4' \
  python -m pytest -q tests/test_support_turn_mysql.py
```

这 4 项测试已在临时 MySQL 8.0 环境中验证通过，覆盖消息、报告、人工审核请求和工具任务写入后的整体回滚。

## MySQL 和 Redis 配置

系统默认使用 MySQL 保存完整业务数据和完整聊天消息，使用 Redis 保存短期对话记忆。启动服务前先创建数据库：

```sql
CREATE DATABASE xling DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'xling'@'%' IDENTIFIED BY 'xling';
GRANT ALL PRIVILEGES ON xling.* TO 'xling'@'%';
FLUSH PRIVILEGES;
```

## 人工审核检查点

LangGraph 默认使用官方 `AsyncSqliteSaver` 保存人工审核中断状态，服务重启后仍可批准或拒绝。检查点只保存纯数据，不使用 pickle；默认保留 30 天。学生删除账号时，业务数据库先提交删除，再清理对应检查点；清理失败会留下可重试任务，并在应用下次启动时继续处理，接口不会在清理完成前宣称全部删除。

```env
LANGGRAPH_CHECKPOINT_BACKEND=async_sqlite
LANGGRAPH_CHECKPOINT_PATH=data/langgraph-checkpoints.db
LANGGRAPH_CHECKPOINT_RETENTION_DAYS=30
```

`memory` 后端只用于测试和显式本地演示。检查点缺失、过期或损坏时仍执行固定安全降级。

`.env` 中配置连接：

```env
DATABASE_URL=mysql+pymysql://xling:xling@127.0.0.1:3306/xling?charset=utf8mb4
REDIS_URL=redis://127.0.0.1:6379/0
REDIS_MEMORY_TTL_SECONDS=86400
REDIS_MEMORY_MAX_MESSAGES=40
```

完整聊天记录写入 MySQL 的 `chat_sessions`、`chat_messages` 等表。Redis 只保存每个会话最近 `REDIS_MEMORY_MAX_MESSAGES` 条短期上下文，并通过 `REDIS_MEMORY_TTL_SECONDS` 自动过期。

## 快速启动 Mock 演示

Mock 模式不需要 Ollama 或模型文件，但仍需要 MySQL 和 Redis，适合先演示完整业务流程。

```bash
cd XLing-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
AI_PROVIDER=mock uvicorn app.main:app --host 127.0.0.1 --port 8080
```

浏览器打开：

```text
http://127.0.0.1:8080
```

默认账号：

```text
student / student123
admin / admin123
```

接口使用 Bearer Token（放在请求头中的登录令牌）。先登录并保存学生、管理员令牌，后面的命令都会复用它们：

```bash
STUDENT_TOKEN=$(curl -s \
  -H 'Content-Type: application/json' \
  -d '{"username":"student","password":"student123"}' \
  http://127.0.0.1:8080/api/auth/login \
  | python -c 'import json,sys; print(json.load(sys.stdin)["accessToken"])')

ADMIN_TOKEN=$(curl -s \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' \
  http://127.0.0.1:8080/api/auth/login \
  | python -c 'import json,sys; print(json.load(sys.stdin)["accessToken"])')
```

## Docker Compose 一键启动

仓库提供 `Dockerfile` 和 `docker-compose.yml`，会启动：

- `mysql`：MySQL 8.4，容器内端口 `3306`，宿主机映射 `13306`
- `redis`：Redis 7，容器内端口 `6379`，宿主机映射 `16379`
- `app`：Xling FastAPI 服务，宿主机端口 `8080`

默认配置会让应用容器访问宿主机 Ollama：

```bash
docker compose up -d --build
```

如果 Ollama 已经有下列模型，容器即可使用真实本地聊天模型链路：

```text
xling-qwen2.5-7b-ft:latest
```

## Chroma 向量库与快照

应用启动时会同步 `app/knowledge/*.md` 内置默认知识库到数据库。当前默认文档覆盖校园心理支持总则、风险等级策略、焦虑恐慌、情绪低落、睡眠作息、学业压力、考试季、人际关系、新生适应、咨询转介和隐私边界等主题；如果默认 md 内容发生变化，重启后对应来源会按当前切块规则刷新入库。

知识库默认优先使用 Chroma 持久化向量库，embedding 由 OpenAI `text-embedding-3-small` 提供。没有 `OPENAI_API_KEY`、缺少 `chromadb` 或向量调用失败时，才回退到本地 `hybrid_score` 检索：

```env
OPENAI_API_KEY=你的_API_Key
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
KNOWLEDGE_VECTOR_ENABLED=true
KNOWLEDGE_VECTOR_REQUIRED=false
CHROMA_PERSIST_DIR=data/chroma
CHROMA_SNAPSHOT_DIR=data/chroma-snapshots
```

管理员接口：

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" http://127.0.0.1:8080/api/admin/knowledge/status
curl -H "Authorization: Bearer $ADMIN_TOKEN" -X POST http://127.0.0.1:8080/api/admin/knowledge/rebuild-vector
curl -H "Authorization: Bearer $ADMIN_TOKEN" -X POST http://127.0.0.1:8080/api/admin/knowledge/backup
```

当 `KNOWLEDGE_VECTOR_REQUIRED=false` 时，如果 Chroma 或 embedding 服务不可用，系统会降级到本地混合检索；设为 `true` 则启动或检索失败时直接暴露错误。

## 工具队列、限流与死信

心理报告生成后，工具链不会阻塞学生端流式回复，而是写入 `tool_jobs` 队列表：

```text
EXCEL_REPORT -> RISK_ALERT
```

Excel 写入使用进程内锁串行化，邮件预警使用独立线程池并支持每分钟限流。失败任务会按延迟重试，超过 `TOOL_QUEUE_MAX_ATTEMPTS` 后进入 `dead_letter_records`。

一轮支持过程中的会话变化、学生消息、心理报告、人工审核请求和工具任务使用一次数据库提交；任一步骤失败都会一起回滚。`tool_jobs` 对“报告编号 + 任务类型”设置唯一约束，worker 使用条件更新原子抢占任务。运行中任务带 5 分钟租约，调度循环只回收已过期任务，避免另一 worker 正在执行时被重复派发。

```env
TOOL_QUEUE_ENABLED=true
TOOL_QUEUE_EXCEL_WORKERS=1
TOOL_QUEUE_EMAIL_WORKERS=2
ALERT_EMAIL_RATE_LIMIT_PER_MINUTE=30
ALERT_EMAIL_DELIVERY_MODE=log
```

`ALERT_EMAIL_DELIVERY_MODE=log` 适合本地演示；生产发邮件时改为 `smtp` 并配置 SMTP。

## 邮件预警配置

高风险消息会触发心理报告，默认由持久化工具队列完成 Excel 台账和邮件预警；关闭工具队列时才通过 MCP 调用。发送邮件前需要在 `.env` 中配置 SMTP：

```env
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=your-account@example.com
SMTP_PASSWORD=your-smtp-password
SMTP_USE_TLS=true
SMTP_USE_SSL=false
ALERT_EMAIL_FROM=your-account@example.com
ALERT_EMAIL_TO=counselor@example.com,admin@example.com
ALERT_EMAIL_SUBJECT_PREFIX=[Xling 高风险预警]
```

未配置 SMTP 或收件人时，系统不会中断聊天流程，但会在 `alert_records` 中写入 `FAILED` 记录，提示缺少的配置项。

## 接入本地微调 GGUF 模型

Python 版默认预留本地模型名：

```text
xling-qwen2.5-7b-ft:latest
```

模型目录：

```text
models/xling-qwen2.5-7b-ft/
```

需要放入的 GGUF 权重：

```text
models/xling-qwen2.5-7b-ft/xling-qwen2.5-7b-ft-q4_k_m.gguf
```

如果本机已经有其他位置的 GGUF 模型文件，可以通过 `UPSTREAM_GGUF` 指定路径并建立软链接：

```bash
UPSTREAM_GGUF=/path/to/xling-qwen2.5-7b-ft-q4_k_m.gguf ./scripts/create-finetuned-model.sh
```

创建 Ollama 模型：

```bash
./scripts/create-finetuned-model.sh
```

启动 Ollama：

```bash
./scripts/start-ollama.sh
```

启动 Python 服务：

```bash
AI_PROVIDER=ollama ./scripts/run-dev.sh
```

查看模型接入状态：

```bash
curl -H "Authorization: Bearer $STUDENT_TOKEN" http://127.0.0.1:8080/api/agent/status
```

返回结果中的 `finetunedModel.ggufExists` 和 `finetunedModel.modelfileExists` 会显示模型资产是否就绪。
同时 `agentFramework.active` 会显示当前实际使用的 Agent 编排框架：

```text
langgraph
custom
```

## 接入 OpenAI-compatible API

```bash
AI_PROVIDER=openai \
OPENAI_API_KEY=你的_API_Key \
OPENAI_MODEL=gpt-4o-mini \
OPENAI_EMBEDDING_MODEL=text-embedding-3-small \
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

知识库向量检索也使用同一个 `OPENAI_API_KEY` 调用 embeddings API。相关配置：

```env
KNOWLEDGE_VECTOR_ENABLED=true
KNOWLEDGE_VECTOR_REQUIRED=false
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
CHROMA_PERSIST_DIR=data/chroma
CHROMA_COLLECTION_NAME=xling_knowledge
```

当 `KNOWLEDGE_VECTOR_REQUIRED=false` 时，缺少 API key 或 Chroma 不可用不会阻断聊天，系统会回退到本地 `hybrid_score` 检索。若交付验收要求必须走 Chroma 向量检索，可设置 `KNOWLEDGE_VECTOR_REQUIRED=true`。

## 接入 DeepSeek API

DeepSeek（深度求索）提供 OpenAI 兼容的对话接口，可以直接复用 `openai` provider，无需改动代码。

在 `.env` 中配置：

```env
AI_PROVIDER=openai
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_API_KEY=你的_DeepSeek_API_Key
OPENAI_MODEL=deepseek-chat
KNOWLEDGE_VECTOR_REQUIRED=false
# 可选：为知识检索单独配置支持 embeddings 的服务
EMBEDDING_BASE_URL=https://api.openai.com/v1
EMBEDDING_API_KEY=你的_Embedding_API_Key
```

或通过环境变量启动：

```bash
AI_PROVIDER=openai \
OPENAI_BASE_URL=https://api.deepseek.com/v1 \
OPENAI_API_KEY=你的_DeepSeek_API_Key \
OPENAI_MODEL=deepseek-chat \
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

可选模型：

- `deepseek-chat`：DeepSeek-V3，通用对话模型，响应快，适合日常心理陪伴对话。
- `deepseek-reasoner`：DeepSeek-R1，推理能力强，适合需要深度分析的场景，响应较慢。

注意事项：

- DeepSeek 不提供 embeddings（向量嵌入）接口。未单独配置嵌入服务时，知识检索会降级到本地混合检索；保持 `KNOWLEDGE_VECTOR_REQUIRED=false` 即可，不影响正常对话和风险检测。
- 如果同时需要 Chroma 向量检索，使用 `EMBEDDING_BASE_URL` 和 `EMBEDDING_API_KEY` 配置独立嵌入服务即可，聊天仍走 DeepSeek，不需要修改代码。
- DeepSeek API Key 在 [DeepSeek 开放平台](https://platform.deepseek.com/) 的「API Keys」页面创建。

验证接入状态：

```bash
curl -H "Authorization: Bearer $STUDENT_TOKEN" http://127.0.0.1:8080/api/agent/status
```

返回结果中的 `aiProvider` 应为 `openai`，`agentFramework.active` 应为 `langgraph`。

## 调用示例

学生流式聊天：

```bash
curl -N -H "Authorization: Bearer $STUDENT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"message":"我最近很焦虑，晚上总是睡不着"}' \
  http://127.0.0.1:8080/api/chat/stream
```

高风险示例，会触发心理报告和人工审核，并通过默认持久化任务队列写入 Excel、发送邮件预警：

```bash
curl -N -H "Authorization: Bearer $STUDENT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"message":"我不想活了，感觉撑不下去了"}' \
  http://127.0.0.1:8080/api/chat/stream
```

管理员查看报告：

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" http://127.0.0.1:8080/api/admin/reports
```

学生查看会话历史列表：

```bash
curl -H "Authorization: Bearer $STUDENT_TOKEN" http://127.0.0.1:8080/api/sessions
```

学生加载某个会话的完整消息：

```bash
curl -H "Authorization: Bearer $STUDENT_TOKEN" http://127.0.0.1:8080/api/sessions/{sessionId}
```

管理员追加知识库：

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"source":"sleep-guide","content":"失眠时可先固定起床时间，减少睡前屏幕刺激，必要时联系校心理中心。"}' \
  http://127.0.0.1:8080/api/admin/knowledge
```

追加知识库时，系统会同步写入 MySQL 分块和 Chroma 向量库；已有分块会在首次向量检索时自动补建 Chroma 索引。

## RAG 评测

```bash
KNOWLEDGE_VECTOR_REQUIRED=true AI_PROVIDER=mock python -m evals.rag.runner hybrid-rrf
pip install -r requirements-bge.txt
BGE_DEVICE=cpu python -m evals.rag.runner bge-m3
BGE_DEVICE=mps python -m evals.rag.runner bge-m3-rerank
python -m evals.rag.runner comparison
```

报告包含模型、提示词、索引、数据和代码版本，以及 Recall@K、MRR、NDCG、95 分位延迟、安全关键片段漏检率和索引大小。候选检索只有同时通过质量、安全和延迟门槛才会建议替换当前默认实现。

当前冻结的 100 条中文难例、Top-5（返回前 5 条）本机结果如下。BGE 在 Apple MPS（苹果芯片图形加速）上运行，重排候选数为 5；当前向量服务返回 400，因而当前检索只能降级到本地混合检索，本轮公平对照门槛明确记为未通过。

| 检索方案 | Recall@5 | MRR | NDCG@5 | 安全片段漏检率 | 第 95 百分位延迟 |
|---|---:|---:|---:|---:|---:|
| 当前混合检索降级路径 | 0.9600 | 0.8758 | 0.8782 | 0% | 68.3 毫秒 |
| BGE-M3 | 0.9900 | 0.9183 | 0.9168 | 3.125% | 116.3 毫秒 |
| BGE-M3 + 专用重排 | 0.9900 | 0.9417 | 0.9387 | 3.125% | 5112.7 毫秒 |

BGE 的质量指标提高，但安全片段漏检率上升，BGE-M3 延迟超过门槛，专用重排延迟更明显；同时当前向量基线未成功建立。因此生产默认保持当前检索，不启用 BGE。要重新作替换决策，必须先修复当前嵌入接口，再在相同数据、切块、Top-5 和模型版本下重跑。

## 轻量端到端评测

40 条案例直接经过生产聊天模块，覆盖日常对话、低中风险支持、明确高风险、知识不足和行动计划：

```bash
python -m evals.e2e.runner
```

## 风险概率校准与共形预测

先按冻结清单收集校准集和最终测试集的风险分数，再运行温度缩放和类别条件共形预测：

```bash
RISK_EVAL_AI_PROVIDER=mock python -c "from evals.risk.calibration_runner import collect_calibration_inputs; collect_calibration_inputs()"
python -m evals.risk.calibration_runner
```

报告比较原始概率和校准概率的高风险召回、Brier 分数与期望校准误差，并记录覆盖率、平均预测集合大小和人工审核率。只有达到预设门槛才允许配置 `RISK_CALIBRATION_ARTIFACT`；否则默认安全风险评估保持不变。

生产分类器必须通过独立分数接口返回 LOW、MEDIUM、HIGH（三档风险）的真实原始分数，才能启用校准：

```env
RISK_SCORE_ENDPOINT=http://127.0.0.1:9000/score
RISK_SCORE_API_KEY=
```

接口响应格式为 `{"label":"低落","logits":[-0.4,2.3,-1.2]}`。没有该接口时不会把离散标签伪装成生产原始分数，运行时会采用保守安全评估。

当前 Mock（规则模拟）探索结果使用 25 条校准数据和 25 条最终测试数据：校准前后高风险召回均为 0.3333，期望校准误差从 0.5497 降至 0.1619，但共形覆盖率为 1.0、平均集合大小为 2.76、转人工比例为 1.0。每类校准样本只有 8～9 条，且没有可验证的同源改写分组，因此报告强制标记为探索结果，不提供覆盖保证，也不生成启用建议。

主要评测产物输出到：

```text
target/rag-eval-report-hybrid-rrf.json
target/rag-eval-bge-m3.json
target/rag-eval-bge-m3-rerank.json
target/rag-eval-comparison.json
target/e2e-eval-report.json
target/risk-calibration-report.json
```

## MCP 工具服务

MCP Python 包建议使用 Python 3.10 或 3.11 安装运行。

```bash
python -m app.mcp_tools.server
```

业务后端默认通过持久化工具队列执行报告后处理。关闭工具队列时，会改用 MCP client 通过 stdio 启动同一个 MCP server；外部工具客户端也可以单独启动这个服务。

暴露工具：

- `xling_excel_report`
- `xling_alert_notify`
