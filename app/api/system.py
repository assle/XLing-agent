from typing import Annotated

from fastapi import APIRouter, Depends

from app.agents.factory import agent_framework_status
from app.agents.runtime import AgentRuntimeService
from app.core.config import get_settings
from app.core.security import current_user
from app.models.entities import UserAccount
from app.services.model_assets import finetuned_model_status
from app.services.risk_trajectory import RiskTrajectoryHealth

router = APIRouter()


@router.get("/actuator/health")
def health():
    return {"status": "UP", "riskTrajectory": RiskTrajectoryHealth.snapshot()}


@router.get("/api/agent/status")
def agent_status(_: Annotated[UserAccount, Depends(current_user)]):
    settings = get_settings()
    provider = settings.ai_provider.lower()
    model = (
        settings.ollama_model
        if provider == "ollama"
        else settings.openai_model
        if provider == "openai"
        else "mock"
    )
    return {
        "provider": provider,
        "model": model,
        "realModelEnabled": provider in {"ollama", "openai"},
        "agentFramework": agent_framework_status(settings),
        "finetunedModel": finetuned_model_status(settings),
        "agents": [
            {"name": "MemoryAgent", "status": "READY", "description": "短期上下文与长期记忆摘要"},
            {"name": "SupervisorAgent", "status": "READY", "description": "消息分流"},
            {"name": "RiskGuardianAgent", "status": "READY", "description": "安全风险评估与分级"},
            {"name": "KnowledgeAgent", "status": "READY", "description": "相关支持内容检索"},
            {"name": "CompanionAgent", "status": "READY", "description": "普通陪伴式回复"},
            {"name": "CounselorAgent", "status": "READY", "description": "咨询式支持回复"},
        ],
        "loop": {"type": "bounded", "maxSteps": AgentRuntimeService.max_steps},
    }
