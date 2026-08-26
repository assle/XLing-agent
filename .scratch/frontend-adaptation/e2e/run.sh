#!/usr/bin/env bash
# 一键重置 E2E 环境并运行 Playwright 验证
# 用法: bash run.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../../.." && pwd)"
cd "$SCRIPT_DIR"

# 停掉旧服务
lsof -ti :8080 | xargs kill -9 2>/dev/null || true
sleep 1

# 全新数据库
python3 -c "import os; os.path.exists('/tmp/xling-e2e.db') and os.remove('/tmp/xling-e2e.db')"

# 启动应用（模拟 AI + custom runtime + sqlite）
cd "$REPO"
DATABASE_URL="sqlite:////tmp/xling-e2e.db" AI_PROVIDER=mock AGENT_FRAMEWORK=custom \
  KNOWLEDGE_VECTOR_ENABLED=false LANGGRAPH_CHECKPOINT_BACKEND=memory \
  REDIS_URL="redis://127.0.0.1:6379/0" \
  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080 > /tmp/xling-e2e-server.log 2>&1 &
SERVER_PID=$!
trap "kill -9 $SERVER_PID 2>/dev/null || true" EXIT

# 等待服务就绪
for i in $(seq 1 30); do
  curl -sf http://127.0.0.1:8080/actuator/health > /dev/null && break
  sleep 0.5
done

# 造一条待审核记录（管理端验证用）
REPO="$REPO" "$REPO/.venv/bin/python" - << 'PYEOF'
import os
import sys
sys.path.insert(0, os.environ["REPO"])
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models.entities import ChatSession, PsychologicalReport, ReviewRequest, UserAccount

engine = create_engine("sqlite:////tmp/xling-e2e.db")
db = sessionmaker(bind=engine)()
student = db.query(UserAccount).filter(UserAccount.username == "student").first()
session = ChatSession(public_id="e2e-review-session", title="高风险会话", user_id=student.id)
db.add(session); db.flush()
report = PsychologicalReport(
    user_id=student.id, session_id=session.id, content="最近压力太大，有过不好的念头",
    intent="RISK", emotion="HIGH_RISK", emotion_score=4.0, risk_level="HIGH",
    confidence=0.95, summary="学生表达高风险情绪，需要人工复核",
)
db.add(report); db.flush()
db.add(ReviewRequest(
    session_id=session.id, report_id=report.id, thread_id="e2e-review-session",
    risk_summary="学生表达高风险情绪，需要人工复核",
    handoff_reason="RISK_TRAJECTORY_RISING",
    desensitized_summary="当前困境：考研冲刺期压力大，睡眠差。\n风险轨迹：3 天内由 LOW 升至 HIGH。\nCBT 摘要：未完成。\n行动计划：无。",
    status="pending",
))
db.commit()
print("seeded review")
PYEOF

# 运行 Playwright 验证
cd "$SCRIPT_DIR"
node verify.mjs
