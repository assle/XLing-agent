from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.runtime import AgentRunResult
from app.core.bootstrap import create_schema, seed_data
from app.core.enums import RiskLevel
from app.core.versioning import ArtifactVersionResolver
from app.models.entities import ChatSession, ReviewRequest, SafetyAssessmentRecord, UserAccount
from app.schemas.dtos import ChatRequest
from app.services.chat import ChatDependencies, ChatService
from evals.config import EvalSettings, get_eval_settings


def evaluate(settings: EvalSettings | None = None) -> dict:
    """在独立内存数据库中运行完整聊天模块的精选验证场景。

    按评估配置选择模型，关闭向量检索并使用进程内执行状态保存；完成后释放数据库资源并写出报告。
    """
    settings = settings or get_eval_settings()
    settings = settings.model_copy(
        update={
            "ai_provider": settings.e2e_eval_ai_provider,
            "knowledge_vector_enabled": False,
            "langgraph_checkpoint_backend": "memory",
        }
    )
    dataset_path = Path(settings.e2e_eval_dataset)
    cases = _load_cases(dataset_path)

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_schema(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = sessions()
    try:
        seed_data(db, settings)
        user = db.query(UserAccount).filter(UserAccount.username == "student").one()
        results = [asyncio.run(_run_case(db, settings, user, case)) for case in cases]
    finally:
        db.close()
        engine.dispose()

    report = {
        "artifactVersion": ArtifactVersionResolver(settings).current(dataset_path).to_dict(),
        "totalCases": len(results),
        "passedCases": sum(1 for result in results if result["passed"]),
        "cases": results,
    }
    output = Path(settings.e2e_eval_output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


async def _run_case(db, settings: EvalSettings, user: UserAccount, case: dict) -> dict:
    """按一个案例的消息顺序执行真实聊天流程并检查保存结果。

    从会话事件取得编号，收集执行步骤和最新评估及审核记录，比较分流、风险、事件顺序与知识访问。
    返回具体失败原因列表；知识顺序检查基于全部步骤中首次评估与首次检索的位置。
    """
    session_id = None
    events: list[str] = []
    # 直接观察执行结果对象，从结构化步骤验证先后关系，不解析面向人的日志描述。
    observed_runs: list[AgentRunResult] = []
    dependencies = replace(
        ChatDependencies.create(db, settings),
        observe_run=observed_runs.append,
    )
    service = ChatService(db, settings, dependencies)
    for message in case["messages"]:
        request = ChatRequest(message=message, sessionId=session_id)
        async for chunk in service.stream_chat(user, request):
            name, payload = _parse_event(chunk)
            if name and (not events or name != events[-1]):
                events.append(name)
            if name == "meta":
                session_id = payload["sessionId"]

    session = db.query(ChatSession).filter(ChatSession.public_id == session_id).one()
    report = (
        db.query(SafetyAssessmentRecord)
        .filter(SafetyAssessmentRecord.session_id == session.id)
        .order_by(SafetyAssessmentRecord.created_at.desc())
        .first()
    )
    review = (
        db.query(ReviewRequest)
        .filter(ReviewRequest.session_id == session.id)
        .order_by(ReviewRequest.created_at.desc())
        .first()
    )
    actual_intent = report.intent if report else "CHAT"
    actual_risk = report.risk_level if report else RiskLevel.LOW.value
    expected_events = case.get("expectedEvents", [])
    actions = [step.action for run in observed_runs for step in run.steps]
    knowledge_used = "RETRIEVE_KNOWLEDGE" in actions
    knowledge_allowed = case.get(
        "knowledgeAccessAllowed",
        case["category"] not in {"chat", "risk"},
    )
    assessment_indexes = [
        index for index, action in enumerate(actions) if action == "ASSESS_RISK"
    ]
    knowledge_indexes = [
        index for index, action in enumerate(actions) if action == "RETRIEVE_KNOWLEDGE"
    ]
    safety_before_knowledge = not knowledge_indexes or (
        bool(assessment_indexes) and assessment_indexes[0] < knowledge_indexes[0]
    )
    failures = []
    if actual_intent != case["expectedIntent"]:
        failures.append(f"intent expected {case['expectedIntent']}, got {actual_intent}")
    if actual_risk not in case.get("allowedRisks", [case["expectedRisk"]]):
        failures.append(f"risk expected {case['expectedRisk']}, got {actual_risk}")
    if bool(review) != bool(case["expectReview"]):
        failures.append(f"review expected {case['expectReview']}, got {bool(review)}")
    if events != expected_events:
        failures.append(f"events expected {expected_events}, got {events}")
    if knowledge_used != knowledge_allowed:
        failures.append(
            f"knowledge access allowed {knowledge_allowed}, got {knowledge_used}"
        )
    if not safety_before_knowledge:
        failures.append("risk assessment did not finish before knowledge retrieval")

    return {
        "id": case["id"],
        "category": case["category"],
        "sessionId": session_id,
        "intent": actual_intent,
        "risk": actual_risk,
        "reviewCreated": bool(review),
        "events": events,
        "agentActions": actions,
        "knowledgeAccessAllowed": knowledge_allowed,
        "knowledgeUsed": knowledge_used,
        "safetyBeforeKnowledge": safety_before_knowledge,
        "passed": not failures,
        "failures": failures,
    }


def _load_cases(path: Path) -> list[dict]:
    """读取每行一个对象的完整流程评估样本。

    跳过空白行，保持文件顺序，格式异常直接向上传递。
    """
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _parse_event(chunk: str) -> tuple[str | None, dict]:
    """从聊天服务发出的单个事件文本提取事件名和数据字典。

    没有对应行时保留 None 或空字典；不是处理任意网络碎片的缓冲解析器。
    """
    name = None
    payload = {}
    for line in chunk.splitlines():
        if line.startswith("event: "):
            name = line.removeprefix("event: ")
        elif line.startswith("data: "):
            payload = json.loads(line.removeprefix("data: "))
    return name, payload


if __name__ == "__main__":
    result = evaluate()
    print(f"totalCases={result['totalCases']}")
    print(f"passedCases={result['passedCases']}")
