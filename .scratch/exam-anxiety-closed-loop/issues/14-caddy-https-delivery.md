Status: ready-for-agent

# 14: Docker + Caddy 自动 HTTPS 交付

## Parent

../spec.md (exam-anxiety-closed-loop)

## What to build

把应用交付拓扑扩展为适合低成本云服务器的 Docker 编排：应用、MySQL、Redis 与 Caddy 一次启动，Caddy 是唯一公网入口，通过用户提供的域名自动签发和续期 HTTPS 证书，并把流式对话正确反向代理到应用。数据库、Redis 和应用内部端口默认不暴露公网；持久数据使用明确卷，敏感配置通过环境变量/部署 secret 提供。

同时提供从域名 DNS、服务器防火墙、环境配置、首次启动到备份/升级验证的精简部署说明。此 issue 不代表用户购买服务器或域名。

## Acceptance criteria

- [x] Docker 编排可在干净服务器构建并启动应用、MySQL、Redis 和 Caddy，依赖健康检查正常
- [x] Caddy 是唯一公开的 HTTP/HTTPS 服务，应用、数据库和 Redis 内部端口默认不绑定公网
- [x] 域名和证书联系邮箱通过环境配置提供，Caddy 自动完成 HTTP 到 HTTPS 跳转及证书签发/续期
- [x] 反向代理支持现有 SSE 长连接，登录、聊天、管理员能力、隐私页和删除入口经 HTTPS 可用
- [x] 数据库、Redis、知识索引及所需应用状态使用持久卷，容器重建后数据不丢失
- [x] JWT secret、数据库密码和 provider key 不写入镜像或版本库，缺失关键生产配置时启动明确失败
- [x] 部署文档覆盖 DNS、防火墙、环境变量、启动、健康检查、日志、备份和升级/回滚检查
- [x] 自动化 smoke test 验证容器健康、内部端口隔离、HTTP 跳转、HTTPS 路由和公开隐私页

## Blocked by

- 01 (`01-secure-access-migration.md`)
- 13 (`13-privacy-and-data-deletion.md`)


## Comments

### 2026-07-22 实现完成

- `docker-compose.yml`：完全重写。Caddy 作为唯一公网入口（80/443），App/MySQL/Redis 端口不暴露公网。JWT_SECRET_KEY 必须设置（缺失时启动失败）。SSE 通过 Caddy 反向代理（flush_interval -1, 300s 超时）。持久卷：mysql-data, redis-data, app-data, caddy-data, caddy-config。
- `Caddyfile`：自动 HTTPS（Let's Encrypt），HTTP->HTTPS 跳转，隐私说明页和健康检查公开可访问，SSE 长连接支持
- `docs/deployment-guide.md`：完整部署文档覆盖 DNS、防火墙、环境变量、启动、健康检查、日志、备份、升级/回滚
- `.env.production.example`：生产环境配置模板
