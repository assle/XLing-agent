#!/usr/bin/env bash
# 创建 Ollama 分类器模型。
#
# 前置：已运行 quantize.sh，gguf 已放在 models/xling-cls-3b-ft/。
#
# 用法：
#   ./scripts/create-classifier-model.sh
set -euo pipefail

# 根据脚本位置定位模型目录，避免依赖调用者当前所在目录。
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_NAME="${CLASSIFIER_MODEL_NAME:-xling-cls-3b-ft:latest}"
MODEL_DIR="$ROOT_DIR/models/xling-cls-3b-ft"
GGUF_FILE="xling-cls-3b-ft-q4_k_m.gguf"

# 自动寻找本地模型服务命令，仍允许使用 OLLAMA_BIN 指定安装位置。
DEFAULT_OLLAMA_BIN="$(command -v ollama || true)"
if [ -z "$DEFAULT_OLLAMA_BIN" ] && [ -x "/Applications/Ollama.app/Contents/Resources/ollama" ]; then
  DEFAULT_OLLAMA_BIN="/Applications/Ollama.app/Contents/Resources/ollama"
fi
OLLAMA_BIN="${OLLAMA_BIN:-$DEFAULT_OLLAMA_BIN}"

# 检查模型服务命令可执行后再继续，避免把缺少命令误判为注册失败。
if [ ! -x "$OLLAMA_BIN" ]; then
  echo "未找到 Ollama。请安装 Ollama 或设置 OLLAMA_BIN。"
  exit 1
fi

# 只注册已经准备好的量化文件，此脚本本身不训练或量化模型。
if [ ! -f "$MODEL_DIR/$GGUF_FILE" ]; then
  echo "缺少 gguf 模型文件: $MODEL_DIR/$GGUF_FILE"
  echo "请先运行: ./finetune/scripts/quantize.sh"
  exit 1
fi

# 按照 Modelfile（模型加载配置文件）把分类器注册到本地服务。
"$OLLAMA_BIN" create "$MODEL_NAME" -f "$MODEL_DIR/Modelfile"

echo "已创建分类器模型: $MODEL_NAME"
echo "启动 Xling: AI_PROVIDER=ollama ./scripts/run-dev.sh"
