#!/usr/bin/env bash
set -euo pipefail

# 定位项目并读取可覆盖的模型名称、目录、文件及上游来源。
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_NAME="${FINETUNED_MODEL_NAME:-xling-qwen2.5-7b-ft:latest}"
MODEL_DIR="${FINETUNED_MODEL_DIR:-$ROOT_DIR/models/xling-qwen2.5-7b-ft}"
GGUF_FILE="${FINETUNED_MODEL_FILE:-xling-qwen2.5-7b-ft-q4_k_m.gguf}"
UPSTREAM_GGUF="${UPSTREAM_GGUF:-}"

# 优先搜索系统命令，再尝试 macOS 应用内置的模型服务程序。
DEFAULT_OLLAMA_BIN="$(command -v ollama || true)"
if [ -z "$DEFAULT_OLLAMA_BIN" ] && [ -x "/Applications/Ollama.app/Contents/Resources/ollama" ]; then
  DEFAULT_OLLAMA_BIN="/Applications/Ollama.app/Contents/Resources/ollama"
fi
OLLAMA_BIN="${OLLAMA_BIN:-$DEFAULT_OLLAMA_BIN}"

if [ ! -x "$OLLAMA_BIN" ]; then
  echo "Cannot find Ollama."
  echo "Install Ollama or set OLLAMA_BIN to the ollama executable path."
  exit 1
fi

mkdir -p "$MODEL_DIR"

# 目标文件缺失时只在上游文件确实存在的情况下建立链接，否则停止并提示准备模型。
if [ ! -f "$MODEL_DIR/$GGUF_FILE" ]; then
  if [ -n "$UPSTREAM_GGUF" ] && [ -f "$UPSTREAM_GGUF" ]; then
    echo "Linking GGUF from upstream Xling project..."
# 使用符号链接复用上游大模型文件，不把整个文件再复制一份。
    ln -sf "$UPSTREAM_GGUF" "$MODEL_DIR/$GGUF_FILE"
  else
    echo "Missing GGUF model file:"
    echo "  $MODEL_DIR/$GGUF_FILE"
    echo
    echo "Put the model file there, or set UPSTREAM_GGUF to an existing GGUF path."
    exit 1
  fi
fi

# 读取模型定义并执行本地注册；成功退出后才打印完成信息。
"$OLLAMA_BIN" create "$MODEL_NAME" -f "$MODEL_DIR/Modelfile"

echo "Created $MODEL_NAME"
echo "Run Xling Python with: AI_PROVIDER=ollama ./scripts/run-dev.sh"
