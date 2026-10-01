from typing import Annotated

from fastapi import APIRouter, Depends

from app.agents.factory import agent_framework_status
from app.core.config import get_settings
from app.core.security import current_user
from app.models.entities import UserAccount
from app.services.model_assets import finetuned_model_status
from app.services.risk_trajectory import RiskTrajectoryHealth

router = APIRouter()


@router.get("/actuator/health")
def health():
    # 返回服务存活标记及风险轨迹组件最近记录的状态。
    # 此接口不逐个测试数据库或模型连通性，UP 表示本接口能够响应。
    return {"status": "UP", "riskTrajectory": RiskTrajectoryHealth.snapshot()}


@router.get("/api/agent/status")
def agent_status(_: Annotated[UserAccount, Depends(current_user)]):
    # 向已登录用户展示当前模型配置和对话处理组件信息。
    # 根据提供方选择模型名称，并汇总图运行方式和模型文件状态。
    # 固定的 READY 标签是组件介绍信息，不能单独证明每个外部服务调用成功。
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
    }
