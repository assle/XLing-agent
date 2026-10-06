# 工程验证与复跑

本轮范围是现有系统的工程收尾：分类器实际接入、固定 RAG 对照、业务路径及可靠性演示。评测问题和演示消息均为工程构造，实际模型、数据库、向量和工具调用使用真实实现。分类器的实验身份、谱系及固定数据结果见 [分类器入口](../finetune/README.md)；RAG 当前结果、本人复核与复跑见 [RAG 评测](../evals/rag/README.md)。

## 演示环境

使用独立 Compose 项目 `xling-closing`，业务数据库为 `xling_closing_test`，网页为 `http://127.0.0.1:18381`。默认通知写日志；SMTP 演示只连接本机接收端及 `example.test` 地址，不转发邮件。回答上限为 4096，分类器使用本机 `xling-general-cls-05b:quoted-f16` 与 quoted 输入；最终业务环境启用真实向量检索及完整索引检查。

本机需要已配置的远程回答与 embedding 凭据、Docker Desktop、本地分类器服务、Microsoft Edge，以及 Playwright/FFmpeg。已验证的依赖版本和分类器 tag 指纹见 [依赖清单](../evals/reports/closing-runtime/dependencies.json)。成功录屏保存在 [assets/closing-videos](../assets/closing-videos/)，随 Git 版本提供；模型权重保存在本机，Git 仓库不包含全部权重。录屏脚本当前使用本机 Codex 捆绑 Node/Playwright 路径，其他电脑需调整该依赖路径。

本轮本地分类器服务使用 Ollama 0.30.8、私有模型目录 `.scratch/closing-runtime/models` 和端口 11434，版本及已选 tag 的 manifest digest 已记录。该模型目录已有选定工件时，可在独立终端执行以下命令启动服务；仅克隆源码不会生成这些权重。

```bash
OLLAMA_MODELS="$PWD/.scratch/closing-runtime/models" \
  .scratch/closing-runtime/ollama-0.30.8/ollama serve
```

已选基座与适配器的来源和包装入口见 [分类器包装](../finetune/README.md#包装)。回答与 embedding 服务的凭据由本机 `.env` 提供，生成上限为 4096；远程服务的模型标识不等同于固定权重身份。

```bash
docker compose -p xling-closing -f docker-compose.yml \
  -f scripts/closing/compose.override.yml build \
  --build-arg HTTP_PROXY= --build-arg HTTPS_PROXY= --build-arg ALL_PROXY= \
  --build-arg http_proxy= --build-arg https_proxy= app
docker compose -p xling-closing -f docker-compose.yml \
  -f scripts/closing/compose.override.yml up -d --no-build app
```

启动后用开发演示账号登录。在后台上传 `evals/rag/closing-deployment-resources.md` 可加载已明确标注的虚构机构资源。真实部署需要替换这些值；本周预约时段是快照，不能解释为真实预约已完成。

## 业务与可靠性路径

| 路径 | 复跑入口 | 成功录屏 | 需观察的结果 |
| --- | --- | --- | --- |
| 日常对话、LOW 支持至计划反馈 | `scripts/closing/browser-support.mjs` | [日常与LOW](../assets/closing-videos/daily-low-support.webm) | 日常直接回复；四维信息完整后生成计划；完成一项并保存提前反馈 |
| MEDIUM 支持至计划反馈 | `scripts/closing/browser-mid-support.mjs` | [MEDIUM](../assets/closing-videos/medium-support.webm) | 实际安全记录含 MEDIUM；计划和反馈持久化 |
| HIGH 暂停、重启与审核恢复 | `scripts/closing/browser-high-review.mjs` | [用户视角](../assets/closing-videos/high-user-restart.webm)、[审核视角](../assets/closing-videos/high-reviewer.webm) | 待审核新消息 409；容器重启仍待审核；批准后恢复；重复决定 404 且无重复消息 |
| 无记忆隔离 | `scripts/closing/browser-no-memory.mjs` | [无记忆](../assets/closing-videos/no-memory.webm) | 新无记忆会话不读确认卡片；当前会话历史保留；诊断日志无长期背景/卡片读取事件 |
| 工具失败、重试与幂等 | `scripts/closing/browser-tool-verified.mjs` | [工具重试](../assets/closing-videos/tool-retry-idempotency.webm) | 本地 SMTP 首次 451；15 秒后自动成功；重复登记和调用后仍为两任务、一台账行、一成功通知 |

脚本使用隔离测试身份；不得改为生产账号。复跑生成的视频及执行结果写入 `.scratch/closing-evidence/`。当前版本固定的六段成功录屏以 [验证清单](../evals/reports/closing-runtime/manifest.json) 中的仓库相对路径及 SHA256 为准；原始结果 JSON 的绝对路径保留拍摄时的执行上下文。脚本返回成功只是流程证据，不代表专业支持或治疗成功；提前反馈只验证保存和分流，不是实际等待 24 小时后的效果。

本机执行脚本的已验证形式为：

```bash
env PLAYWRIGHT_BROWSERS_PATH=/Users/assle/dev/mindbridge-py/.scratch/closing-runtime/playwright \
  /Users/assle/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node \
  scripts/closing/browser-support.mjs
```

其他浏览器入口替换最后的脚本名；涉及容器重启的 HIGH 脚本应在其他请求完成后执行。工具演示先在另一终端运行 `.venv/bin/python scripts/closing/smtp-sink.py`，再用同一项目和基础覆盖文件加 `-f scripts/closing/compose.smtp.yml` 启动 app。将 `scripts/closing/tool-idempotency.py` 复制到容器 `/tmp/closing-tool-idempotency.py` 后运行工具录屏脚本；结束时恢复基础覆盖文件的 app，并停止本机接收端。

## 验证边界

自动检查使用 README 的 Python、前端、Ruff、mypy 和独立 MySQL 事务入口。当前成功结果及实际运行配置见 [验证清单](../evals/reports/closing-runtime/manifest.json)。未配置测试数据库的本机 pytest 跳过 MySQL 用例，独立容器运行与这些跳过分开记录。

本轮 40 条 RAG 与 12 对回答不替代原至少 100 条及全策略产品验收。分类器仍为合成固定数据上的实验性候选，已知四条焦虑误报和人审/临床资格限制保持有效。相关原产品任务只有全部现行要求被验证后才关闭，本轮不据此宣布完整产品或临床资格验收通过。

## 版本交付

工程冻结版本在 `codex/project-closing` 分支维护，交付时使用该版本的 Git 提交或标签。源码、测试、固定评测结果和上述六段成功录屏随版本保存；本机密钥、业务数据库、临时实验目录和模型权重不属于源码交付内容。

从选定标签生成对应归档的命令为：

```bash
mkdir -p target
git archive --format=tar.gz --prefix=xling/ \
  --output=target/xling-engineering-closing-2026-10-06.tar.gz \
  engineering-closing-2026-10-06
```

归档文件与其 SHA256 保存在本机 `target/`，接收方须按前述依赖清单准备模型工件和服务凭据。所测运行镜像及原始评测报告记录的是测试发生时的版本身份；Git 冻结版本新增了固定录屏入口和依赖说明，原始推理结果保持不变。
