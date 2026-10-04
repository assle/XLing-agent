# Xling 部署指南

## 前提条件

- 云服务器（推荐 2C4G+）
- 域名（已解析到服务器 IP）
- Docker + Docker Compose 已安装

## 快速部署

### 1. 克隆代码

```bash
git clone <repo-url> xling
cd xling
```

### 2. 配置环境变量

```bash
cp .env.production.example .env
# 编辑 .env，填写以下必需项：
# - DOMAIN=your-domain.com
# - CADDY_EMAIL=your-email@example.com
# - JWT_SECRET_KEY=<随机 32+ 字符密钥>
# - MYSQL_PASSWORD=<强密码>
# - MYSQL_ROOT_PASSWORD=<强密码>
# - OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL=<同一远程回答服务的配置>
# - OLLAMA_BASE_URL / OLLAMA_CLASSIFIER_MODEL=<容器可访问的本地风险分类器>
```

生产样例默认使用 `AI_PROVIDER=openai`：回答依赖兼容 OpenAI 的远程接口，风险分类仍依赖 Ollama 中已注册的独立分类器。模型角色、请求契约和知识向量配置见 [README 的真实模型联调](../README.md#真实模型联调)。

Docker Desktop 可用 `http://host.docker.internal:11434` 访问宿主上的 Ollama。Linux 服务器需把 `OLLAMA_BASE_URL` 改为应用容器实际可达的地址；当前 Compose 没有为 Linux 配置该宿主别名。分类模型必须已注册，并允许应用容器访问其服务。

### 3. 启动服务

```bash
docker compose up -d --build
```

Caddy 会自动为你的域名签发 HTTPS 证书（通过 Let's Encrypt）。

### 4. 验证

- 访问 `https://your-domain.com` 查看应用
- 访问 `https://your-domain.com/api/privacy` 查看隐私说明（无需登录）
- 访问 `https://your-domain.com/actuator/health` 检查接口存活状态

健康接口的 `UP` 表示接口可以响应，不逐个验证数据库、远程回答模型或本地分类器；管理页面中的 `READY` 也只是组件说明。实际联调需确认普通回复、心理支持分类、高风险暂停与人工审核恢复均能完成，分类输出与失败位置可按 [ADR-0012](adr/0012-langgraph-runtime-and-local-diagnostics.md) 查看执行诊断。

## 架构

```
Internet -> Caddy (80/443, auto-HTTPS) -> App (8080, internal)
                                          -> MySQL (3306, internal)
                                          -> Redis (6379, internal)
```

- Caddy 是唯一公网入口，自动完成 HTTP -> HTTPS 跳转和证书签发/续期
- App、MySQL、Redis 的端口默认不绑定公网
- SSE 长连接通过 Caddy 反向代理（禁用缓冲，300s 超时）
- 持久数据使用 Docker 卷（mysql-data, redis-data, app-data, caddy-data）

## 安全配置

- `JWT_SECRET_KEY` 必须设置（缺失时启动失败）
- `MYSQL_PASSWORD` / `MYSQL_ROOT_PASSWORD` 必须使用强密码
- `AI_PROVIDER=openai` 时，`OPENAI_API_KEY` 是远程回答服务所需凭据；知识向量可通过 `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` 单独配置，留空时使用回答接口配置
- 密码使用 bcrypt 存储，登录使用 24h JWT

## 备份

```bash
# 备份 MySQL
docker compose exec -T mysql sh -c 'exec mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction "$MYSQL_DATABASE"' > backup.sql

# 备份应用数据（知识库、Excel 台账等）
docker run --rm -v xling_app-data:/data -v $(pwd):/backup alpine tar czf /backup/app-data.tar.gz /data
```

## 升级

常规代码升级先备份数据，再更新并重建应用，不执行会话重置：

```bash
git pull
docker compose build app
docker compose up -d app
```

从旧共享上下文切换到原生 LangGraph 状态的本地迁移，按 [ADR-0012](adr/0012-langgraph-runtime-and-local-diagnostics.md#本地切换) 进行一次离线重置，清空旧会话后从新请求联调。先确认业务数据库和 Redis 位于本机，并让应用及其工作任务随应用进程实际退出，保持数据库和 Redis 可用。从项目根目录运行：

```bash
.venv/bin/python -m app.services.local_reset
```

命令清理旧会话及关联安全评估、审核、任务、行动计划和反馈，清理对应会话缓存、配置检查点及 WAL/SHM、失效清理任务和本地台账导出。账户、支持背景、记忆卡片、独立筛查和知识、模型、配置继续保留；不会清空整个 Redis、数据目录或数据卷。

返回 JSON 的 `completed=true` 且退出码为 0，才表示各存储核验完成。失败会报告阶段与固定原因或异常类别；SQL 未提交时保留退休编号供离线重试。重启后使用不携带旧会话编号的新请求；待审核会话暂停发送，审核结束后界面自动恢复。

## 回滚

```bash
git checkout <previous-tag>
docker compose build app
docker compose up -d app
```

## 日志

```bash
# 查看所有服务日志
docker compose logs -f

# 查看特定服务
docker compose logs -f app
docker compose logs -f caddy
```

## 防火墙

确保服务器防火墙只开放 80 和 443 端口：

```bash
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable
```
