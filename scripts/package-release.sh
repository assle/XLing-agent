#!/usr/bin/env bash
set -euo pipefail

# 用时间戳区分本次临时目录和压缩包，所有产物放在 dist 下。
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_NAME="xling"
STAMP="$(date +%Y%m%d-%H%M%S)"
DIST_DIR="$ROOT_DIR/dist"
STAGE_DIR="$DIST_DIR/stage-$STAMP"
ARCHIVE="$DIST_DIR/${PROJECT_NAME}-app-$STAMP.tar.gz"

# 准备打包目录，并清理当前时间戳对应的临时暂存目录。
mkdir -p "$DIST_DIR"
rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR/$PROJECT_NAME"

# 复制应用文件时排除列出的密钥、运行数据、缓存和大模型文件；此列表不是任意文件的敏感信息扫描。
rsync -a "$ROOT_DIR/" "$STAGE_DIR/$PROJECT_NAME/" \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude '.idea/' \
  --exclude '.vscode/' \
  --exclude '.env' \
  --exclude '.env.local' \
  --exclude '.env.production' \
  --exclude '.env.development' \
  --exclude '.env.test' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude 'target/' \
  --exclude 'dist/' \
  --exclude 'data/' \
  --exclude '.DS_Store' \
  --exclude '*.log' \
  --exclude '*.pem' \
  --exclude '*.key' \
  --exclude '*secret*' \
  --exclude '*token*' \
  --exclude '*.db' \
  --exclude '*.sqlite' \
  --exclude '*.sqlite3' \
  --exclude '*.xlsx' \
  --exclude '*.gguf' \
  --exclude '*.gguf.zip' \
  --exclude '*.zip' \
  --exclude '*.tar.gz'

(
  cd "$STAGE_DIR"
# 从暂存目录压缩项目，统一归档所有者并忽略额外文件属性，便于分发。
  COPYFILE_DISABLE=1 tar \
    --no-xattrs \
    --uid 0 --gid 0 \
    --uname root --gname root \
    -czf "$ARCHIVE" "$PROJECT_NAME"
)

# 压缩完成后仅删除本次暂存目录，保留生成的压缩包。
rm -rf "$STAGE_DIR"

echo "Created release package:"
echo "$ARCHIVE"
echo "Model GGUF is intentionally excluded. Send the model zip separately."
du -sh "$ARCHIVE"
