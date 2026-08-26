#!/usr/bin/env bash
# 量化微调分类器模型为 Q4_K_M gguf。
#
# 前置：已完成 LoRA 合并（merge_lora.yaml），产物在 finetune/saves/qwen25-3b-cls-merged。
# 依赖：llama.cpp（含 convert_hf_to_gguf.py 和 llama-quantize）。
#
# 用法：
#   ./finetune/scripts/quantize.sh
#   LLAMA_CPP_DIR=/path/to/llama.cpp ./finetune/scripts/quantize.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MERGED_DIR="${MERGED_DIR:-$ROOT_DIR/finetune/saves/qwen25-3b-cls-merged}"
LLAMA_CPP_DIR="${LLAMA_CPP_DIR:-$ROOT_DIR/../llama.cpp}"
MODEL_DIR="$ROOT_DIR/models/xling-cls-3b-ft"
GGUF_FILE="xling-cls-3b-ft-q4_k_m.gguf"
TMP_GGUF="$ROOT_DIR/finetune/saves/xling-cls-3b-ft-fp16.gguf"

if [ ! -d "$MERGED_DIR" ]; then
  echo "未找到合并后的模型: $MERGED_DIR"
  echo "请先执行合并: llamafactory-cli export finetune/configs/merge_lora.yaml"
  exit 1
fi

if [ ! -d "$LLAMA_CPP_DIR" ]; then
  echo "未找到 llama.cpp: $LLAMA_CPP_DIR"
  echo "请克隆: git clone https://github.com/ggerganov/llama.cpp"
  exit 1
fi

mkdir -p "$MODEL_DIR"

echo "1/2 转换 HF 模型为 gguf (fp16)..."
python "$LLAMA_CPP_DIR/convert_hf_to_gguf.py" "$MERGED_DIR" --outfile "$TMP_GGUF"

echo "2/2 量化为 Q4_K_M..."
"$LLAMA_CPP_DIR/llama-quantize" "$TMP_GGUF" "$MODEL_DIR/$GGUF_FILE" q4_k_m

echo "完成: $MODEL_DIR/$GGUF_FILE"
echo "创建 Ollama 模型: ./scripts/create-classifier-model.sh"
