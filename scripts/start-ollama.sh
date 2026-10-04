#!/usr/bin/env bash
set -euo pipefail

# 先从命令搜索路径寻找本地模型服务，再尝试 macOS 应用内置的可执行文件。
DEFAULT_OLLAMA_BIN="$(command -v ollama || true)"
if [ -z "$DEFAULT_OLLAMA_BIN" ] && [ -x "/Applications/Ollama.app/Contents/Resources/ollama" ]; then
  DEFAULT_OLLAMA_BIN="/Applications/Ollama.app/Contents/Resources/ollama"
fi
# 允许环境变量覆盖可执行文件和监听地址，方便不同机器使用各自安装位置。
OLLAMA_BIN="${OLLAMA_BIN:-$DEFAULT_OLLAMA_BIN}"
OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"
OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://127.0.0.1:11434}"

# 启动前检查目标是否可执行，缺少依赖时给出说明并退出。
if [ ! -x "$OLLAMA_BIN" ]; then
  echo "Cannot find Ollama."
  echo "Install Ollama or set OLLAMA_BIN to the ollama executable path."
  exit 1
fi

# 先查询模型列表接口；已有服务可响应时直接退出，避免重复启动。
if curl -fsS "$OLLAMA_BASE_URL/api/tags" >/dev/null 2>&1; then
  echo "Ollama is already running at $OLLAMA_BASE_URL"
  exit 0
fi

echo "Starting Ollama at $OLLAMA_HOST ..."
# 仅为新进程设置监听地址，并把当前脚本替换为模型服务进程。
exec env OLLAMA_HOST="$OLLAMA_HOST" "$OLLAMA_BIN" serve
