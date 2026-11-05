# Docker 操作指南

本文适用于项目根目录下的 `docker-compose.yml`。所有命令均在项目根目录执行，本机只需要 Docker 和 Docker Compose，不需要安装 Python、MySQL、Redis 或 Ollama。正式部署还应阅读 [部署指南](deployment-guide.md)。

## 配置由哪里负责

- `app/core/config.py` 的 `Settings` 提供稳定的应用默认值、类型校验、派生地址和相对路径解析。不要为了复制默认值而修改应用代码。
- 项目根目录的 `.env` 只保存当前部署需要覆盖的值和密钥。应用容器通过 `env_file` 读取它，Compose 也用它替换服务端口、数据库和模型注册变量。
- `docker-compose.yml` 负责容器拓扑，并设置容器内的数据库、Redis 和 Ollama 地址。它不是应用默认值的另一份副本。
- 进程监听地址由启动器负责。宿主机脚本与容器启动命令的监听配置见下文，不属于共享应用设置。

未在 `.env` 中覆盖的应用项继续跟随 `Settings` 的新默认值。相对模型和数据路径以项目根目录解析，绝对路径保持不变。

## 首次配置与无模型启动

```bash
cp .env.example .env
docker compose up -d --build
```

已有 `.env` 时不要覆盖。本地样例使用 `AI_PROVIDER=mock`，回复由固定规则模拟，不需要模型权重或模型服务。`JWT_SECRET_KEY` 的样例值只用于本地演示；正式使用前必须换成随机且足够长的密钥。

应用、MySQL、Redis 和 Caddy 启动后访问 `https://localhost`。浏览器可能提示确认本地证书。开发演示账号为 `student` / `student123` 和 `admin` / `admin123`，正式部署前必须删除或更换。

健康检查：

```bash
docker compose exec app curl -fsS http://127.0.0.1:8080/actuator/health
```

响应应包含 `"status":"UP"`。它表示接口可以响应，不代表远程模型或真实专业支持服务已经验收。

## 真实模型联调

真实模型联调使用远程回答模型、应用内决策头主分类器，以及 Docker 中的 Ollama 生成式分类器复核与回退路径：

```bash
docker compose --profile models up -d --build
```

在 `.env` 中填写同一 OpenAI 兼容回答服务的配置：

```dotenv
AI_PROVIDER=openai
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=在本机填写有效密钥
OPENAI_MODEL=gpt-4o-mini
```

`AI_MAX_TOKENS` 默认是 2048，只在确需调整单次回答生成上限时覆盖。调高会增加响应时间和模型费用。`ollama-init` 是一次性模型注册容器，成功后显示 `Exited (0)` 是正常状态。

### 回答模型与生成式分类器

- `OLLAMA_MODEL` 是本地回答模型唯一的名称配置，应用请求、模型注册、状态与产物记录都使用它。只有 `AI_PROVIDER=ollama` 时才注册和使用回答模型。
- `OLLAMA_CLASSIFIER_MODEL` 是生成式回退分类器名称，默认 `xling-general-cls-05b:quoted-f16`；`CLASSIFIER_INPUT_FORMAT=quoted` 与其训练输入契约对应。它不同于 `OLLAMA_MODEL`，不要互相赋值。
- Compose 把应用容器内的 `OLLAMA_BASE_URL` 固定为 `http://ollama:11434`。直接在宿主机启动时，应用默认使用 `http://localhost:11434`，可通过环境变量覆盖。
- `OLLAMA_CLASSIFIER_MODEL_DIR` / `OLLAMA_CLASSIFIER_MODEL_FILE` 和 `OLLAMA_ANSWER_MODEL_DIR` / `OLLAMA_ANSWER_MODEL_FILE` 选择注册所用的模型目录与定义文件，不是另一套运行时模型名称。
- `OLLAMA_IMAGE` 可覆盖 Ollama 镜像地址。项目 `models/` 只读挂载，注册结果保存在 `ollama-data` 数据卷。

修改模型注册配置后，重新执行真实模型启动命令。需要检查注册输出时运行：

```bash
docker compose --profile models logs ollama-init
```

### 决策头模型包

默认 `CLASSIFIER_BACKEND=decision_head` 在应用进程内执行单次前向分类。部署方只配置一个模型包目录：

```dotenv
DECISION_HEAD_BUNDLE_DIR=models/xling-decision-head
```

模型包必须采用以下固定结构：

```text
models/xling-decision-head/
├── base/                 # Hugging Face 基座模型
├── adapter/              # LoRA 适配器
├── head.pt               # 决策头权重
├── calibration.json      # 温度校准数据
└── system-prompt.txt     # 系统提示
```

这些权重不随源码分发。权重缺失或加载失败不会阻断启动，也不会改变既有安全语义：分类仍回退到 Ollama 生成式路径并记录降级事件。纯 Ollama 部署可设置 `CLASSIFIER_BACKEND=ollama`，避免重复尝试加载决策头。分层阈值仍由 `CLASSIFIER_HIGH_RISK_THRESHOLD`、`CLASSIFIER_HIGH_RISK_REVIEW_THRESHOLD`、`CLASSIFIER_LOW_CONFIDENCE_THRESHOLD` 和 `CLASSIFIER_MARGIN_THRESHOLD` 控制，校准概率、复核顺序和保守回退见 [ADR-0016](adr/0016-decision-head-classifier-layered-safety.md)。

### 知识向量

如需启用真实知识向量检索，配置同一或独立的 OpenAI 兼容向量服务：

```dotenv
KNOWLEDGE_VECTOR_ENABLED=true
KNOWLEDGE_VECTOR_REQUIRED=false
EMBEDDING_BASE_URL=https://your-embedding-provider.example/v1
EMBEDDING_API_KEY=在本机填写有效密钥
OPENAI_EMBEDDING_MODEL=向量模型名称
```

`EMBEDDING_BASE_URL` 和 `EMBEDDING_API_KEY` 留空时分别复用回答服务地址和密钥。`KNOWLEDGE_VECTOR_REQUIRED=false` 允许向量接口不可用时回退本地检索。应用启动会核对知识正文与索引，并补齐缺失或已变化的条目。

### 环境模式

`ENVIRONMENT` 可取 `local`、`test`、`production`，默认 `local`。生产部署必须显式设置 `ENVIRONMENT=production`，并提供非开发默认值的 JWT 与 MySQL 凭据；缺失密钥、开发默认密钥或开发默认 MySQL 账号密码会在配置构造时直接报错。`test` 用于隔离测试配置，不应作为生产部署的绕过项。

## 使用 DBeaver 连接 MySQL

MySQL 容器使用 `MYSQL_DATABASE`、`MYSQL_USER`、`MYSQL_PASSWORD` 和 `MYSQL_ROOT_PASSWORD`。Compose 根据前三项生成应用容器内的 `DATABASE_URL`，不需要在 `.env` 中重复维护；管理员密码同时用于容器健康检查。

容器和宿主机工具使用不同地址：

| 使用位置 | Server Host | Port | Database / Username / Password |
| --- | --- | --- | --- |
| 应用容器 | `mysql` | `3306` | Compose 从 `MYSQL_*` 生成 |
| 宿主机 DBeaver | `127.0.0.1` | `.env` 的 `MYSQL_PORT`，默认 `3306` | 对应的 `MYSQL_DATABASE` / `MYSQL_USER` / `MYSQL_PASSWORD` |

`mysql` 主机名只在 Docker 网络中有效，不能用于宿主机 DBeaver。`MYSQL_PORT` 是映射到宿主机的端口，只绑定 `127.0.0.1`，不向局域网公开数据库；它不是容器内 MySQL 的监听端口。

修改 `.env` 后执行 `docker compose up -d --force-recreate mysql app` 使端口或 Compose 派生配置生效。已有 `mysql-data` 数据卷中的账号不会因为修改环境变量而自动改密；需要改密时按 MySQL 管理流程处理，不要把重建容器当作数据卷迁移。

## 宿主机启动与监听参数

`scripts/run-dev.sh` 默认监听 `127.0.0.1:8080`。仅在本次进程启动时覆盖：

```bash
SERVER_HOST=0.0.0.0 SERVER_PORT=8080 ./scripts/run-dev.sh
```

该脚本不会把 `.env` 中的 `SERVER_HOST` / `SERVER_PORT` 导出给 shell，因此不要把它们写回共享环境模板。脚本也不再为 `AI_PROVIDER`、`OLLAMA_BASE_URL` 或 `OLLAMA_MODEL` 补 shell 默认值，应用会正常读取 `.env` 中的显式覆盖。

Docker 镜像启动命令固定监听 `0.0.0.0:8080`，由 Caddy 转发请求，不读取上述两个脚本变量。

## 从旧配置迁移

先备份当前 `.env`，再按下表修改。项目不保留旧名称的永久兼容别名。

| 旧配置 | 当前做法 |
| --- | --- |
| `DECISION_HEAD_MODEL_DIR` | 移到模型包的 `base/`，只设置 `DECISION_HEAD_BUNDLE_DIR` |
| `DECISION_HEAD_ADAPTER_DIR` | 移到模型包的 `adapter/` |
| `DECISION_HEAD_HEAD_PATH` | 命名为模型包内的 `head.pt` |
| `DECISION_HEAD_CALIBRATION_PATH` | 命名为模型包内的 `calibration.json` |
| `DECISION_HEAD_SYSTEM_PROMPT_FILE` | 命名为模型包内的 `system-prompt.txt` |
| `FINETUNED_MODEL_NAME` | 改用 `OLLAMA_MODEL` |
| `APP_ENV` | 改用 `ENVIRONMENT=local/test/production` |
| 共享模板中的 `SERVER_HOST` / `SERVER_PORT` | 删除；仅作为 `run-dev.sh` 的进程环境变量使用 |
| 为 Compose 手写的 `DATABASE_URL` | 删除；由 `MYSQL_DATABASE` / `MYSQL_USER` / `MYSQL_PASSWORD` 派生 |

旧本地模板中成组复制的调参、邮件、队列、检索和评测输出默认值也应删除，但不代表对应 `Settings` 或评测设置已失效。确实需要覆盖时，使用当前代码支持的同名变量。旧 DeepSeek 示例不再需要独立的 `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL`，OpenAI 兼容服务统一使用 `OPENAI_BASE_URL` / `OPENAI_MODEL` / `OPENAI_API_KEY`。

修改 Compose、端口或派生地址后必须重建受影响的容器；仅重启旧进程不会更新容器环境。数据库卷、模型卷和应用数据卷应按既有备份流程保留。

## 查看状态与日志

```bash
# 当前 Compose 项目
docker compose ps -a

# 所有名称含 xling 的容器，包括历史遗留容器
docker ps -a --filter name=xling

# 应用日志
docker compose logs -f --tail 100 app

# Ollama 日志
docker compose logs -f --tail 100 ollama
```

按 `Ctrl+C` 只退出日志查看，不会停止容器。开发联调同时把结构化执行记录写入 `data/logs/execution.jsonl`；可按 `run_id` 定位节点、分支、回复和保存阶段。诊断不保存用户正文、完整提示或模型回答，输出故障不阻断业务，详见 [ADR-0012](adr/0012-langgraph-runtime-and-local-diagnostics.md)。

## 修改后重启

```bash
# 重新构建并重启应用
docker compose up -d --build app

# 仅修改 Compose 或环境变量时，强制重建应用容器
docker compose up -d --force-recreate app
```

项目配置文件不保存代理地址。单次构建确需代理时，在命令前临时设置 `HTTP_PROXY`、`HTTPS_PROXY`；需要清除 Docker Desktop 自动注入的代理时，使用命令行构建参数 `HTTP_PROXY=`、`HTTPS_PROXY=`、`http_proxy=`、`https_proxy=`，不要写入 `.env` 或 Compose。

## 运行完整测试

测试使用独立的 `mysql-test` 容器和临时内存数据库，不读写业务数据卷：

```bash
docker compose --profile test build tests
docker compose --profile test run --rm tests
docker compose --profile test stop mysql-test
docker compose --profile test rm -f mysql-test
```

只运行 MySQL 事务测试时，将第二行的 `tests` 换成 `mysql-tests`。测试容器中的 `MYSQL_TEST_DATABASE_URL` 指向 `mysql-test:3306/xling_test`，测试用户只拥有该库的权限。测试会重建表结构，数据库名必须以 `_test` 结尾；测试数据使用临时内存文件系统，不挂载业务 `mysql-data`，也不会随普通 `docker compose up` 启动。

本机直接运行 pytest 且未显式配置 `MYSQL_TEST_DATABASE_URL` 时，MySQL 用例自动启动 `mysql-test`，连接 `127.0.0.1:13306`，并在会话结束后停止和删除该容器；显式配置外部测试库时不自动管理其生命周期。

普通测试配置统一通过 `tests.support.make_test_settings(**overrides)` 构造。工厂禁用部署 `.env` 读取，默认使用 mock 模型、SQLite 内存数据库、内存检查点、关闭向量检索与后台队列，并把 Chroma、台账、检查点和诊断路径放在测试临时目录。测试只传入当前场景需要覆盖的字段；验证应用自身默认值和环境加载的契约测试仍直接使用 `Settings`。

可选的本机静态检查与浏览器测试：

```bash
.venv/bin/ruff check app evals tests
.venv/bin/mypy --ignore-missing-imports app evals
node --experimental-vm-modules --test tests/*-ui.test.mjs
git diff --check
```

## 停止与清理

只暂停当前应用，保留容器和数据：

```bash
docker compose --profile models stop
```

重新启动：

```bash
docker compose --profile models start
```

删除当前项目的容器和网络，保留数据库、模型及应用数据卷：

```bash
docker compose --profile models --profile test down
```

只删除已经完成的一次性模型注册容器：

```bash
docker compose --profile models rm -f ollama-init
```

删除历史遗留容器前，先确认其状态为 `Exited`，再按明确名称删除，避免误删其他项目：

```bash
docker ps -a --filter name=xling
docker rm <已退出的容器名称>
```

以下命令会同时删除项目数据卷，包括 MySQL 业务数据和已注册的 Ollama 模型，**不要作为日常清理命令使用**：

```bash
docker compose --profile models --profile test down -v
```
