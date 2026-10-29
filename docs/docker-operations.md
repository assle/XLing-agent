# Docker 操作指南

本文适用于项目根目录下的 `docker-compose.yml`。所有命令均在项目根目录执行，本机只需要 Docker 和 Docker Compose，不需要安装 Python、MySQL、Redis 或 Ollama。

## 首次配置

```bash
cp .env.example .env
```

已有 `.env` 时不要覆盖。正式使用前设置随机且足够长的 `JWT_SECRET_KEY`；真实模型联调还需填写远程回答接口和分类器配置。

## 启动应用

无真实模型的演示模式：

```bash
docker compose up -d --build
```

真实模型联调模式（远程回答模型 + Docker 中的 Ollama 分类器）：

```bash
docker compose --profile models up -d --build
```

应用启动后访问 `https://localhost`。浏览器可能提示确认本地证书。

`ollama-init` 是一次性模型注册容器，成功后会显示 `Exited (0)`，不是异常，也不需要长期运行。Ollama 默认使用国内镜像 `docker.1ms.run/ollama/ollama:0.1.48`，Python 依赖默认使用阿里云镜像源。

如果远程接口需要宿主机代理，默认地址为 `http://host.docker.internal:7897`。代理端口不同时，在 `.env` 中修改 `HTTP_PROXY`、`HTTPS_PROXY`、`http_proxy` 和 `https_proxy`；MySQL、Redis 和 Ollama 已通过 `NO_PROXY` 绕过代理。

## 查看状态与日志

```bash
# 当前 Compose 项目
docker compose ps -a

# 所有名称含 xling 的容器，包括历史遗留容器
docker ps -a --filter name=xling

# 健康检查
docker compose exec app curl -fsS http://127.0.0.1:8080/actuator/health

# 应用日志
docker compose logs -f --tail 100 app

# Ollama 日志
docker compose logs -f --tail 100 ollama
```

健康检查应返回包含 `"status":"UP"` 的 JSON。按 `Ctrl+C` 只退出日志查看，不会停止容器。

## 修改后重启

```bash
# 重新构建并重启应用
docker compose up -d --build app

# 仅修改 Compose 或环境变量时，可强制重建应用容器
docker compose up -d --force-recreate app
```

修改模型注册配置后，重新执行真实模型启动命令；需要检查注册输出时运行：

```bash
docker compose --profile models logs ollama-init
```

## 运行完整测试

测试使用独立的 `mysql-test` 容器和临时内存数据库，不读写业务数据卷：

```bash
docker compose --profile test build tests
docker compose --profile test run --rm tests
docker compose --profile test stop mysql-test
docker compose --profile test rm -f mysql-test
```

只运行 MySQL 事务测试时，将第二行的 `tests` 换成 `mysql-tests`。

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
