#!/usr/bin/env bash
set -euo pipefail

# 以项目根目录启动，确保应用代码和相对配置路径能够被找到。
cd "$(dirname "$0")/.."
# 监听地址和端口允许由环境覆盖；未设置时仅监听本机 8080 端口。
HOST="${SERVER_HOST:-127.0.0.1}"
PORT="${SERVER_PORT:-8080}"
# 为模型提供方和本地模型补默认值，并导出给随后启动的应用进程。
export AI_PROVIDER="${AI_PROVIDER:-ollama}"
export OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
export OLLAMA_MODEL="${OLLAMA_MODEL:-xling-qwen2.5-7b-ft:latest}"
# 用应用进程替换当前脚本进程，使退出信号直接传递给网页服务。
exec uvicorn app.main:app --host "$HOST" --port "$PORT"
