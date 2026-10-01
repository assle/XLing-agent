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
```

### 3. 启动服务

```bash
docker compose up -d
```

Caddy 会自动为你的域名签发 HTTPS 证书（通过 Let's Encrypt）。

### 4. 验证

- 访问 `https://your-domain.com` 查看应用
- 访问 `https://your-domain.com/api/privacy` 查看隐私说明（无需登录）
- 访问 `https://your-domain.com/actuator/health` 检查健康状态

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
- `OPENAI_API_KEY` 可选（用于向量检索和 LLM 调用）
- 密码使用 bcrypt 存储，登录使用 24h JWT

## 备份

```bash
# 备份 MySQL
docker compose exec mysql mysqldump -uroot -p$MYSQL_ROOT_PASSWORD xling > backup.sql

# 备份应用数据（知识库、Excel 台账等）
docker run --rm -v xling_app-data:/data -v $(pwd):/backup alpine tar czf /backup/app-data.tar.gz /data
```

## 升级

本地 LangGraph 切换使用离线重置，清空旧会话后从新请求联调。先确认业务数据库和 Redis 位于本机，并让应用及其工作任务随应用进程实际退出，保持数据库和 Redis 可用。从项目根目录运行：

```bash
.venv/bin/python -m app.services.local_reset
```

命令清理旧会话及关联安全评估、审核、任务、行动计划和反馈，清理对应会话缓存、配置检查点及 WAL/SHM、失效清理任务和本地台账导出。账户、支持背景、记忆卡片、独立筛查和知识、模型、配置继续保留；不会清空整个 Redis、数据目录或数据卷。

返回 JSON 的 `completed=true` 且退出码为 0，才表示各存储核验完成。失败会报告阶段与固定原因或异常类别；SQL 未提交时保留退休编号供离线重试。重启后使用不携带旧会话编号的新请求；待审核会话暂停发送，审核结束后界面自动恢复。

```bash
git pull
docker compose build app
docker compose up -d app
```

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
