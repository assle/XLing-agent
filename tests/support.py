from __future__ import annotations

from fastapi import APIRouter, FastAPI
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.agents.runtime import AgentRuntime, AgentRuntimeDependencies
from app.core.config import Settings
from app.core.database import Base, get_db
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient
from app.services.assessment import PsychologicalAssessmentService


class DatabaseHarness:
    """One reusable in-memory database setup for tests."""

    def __init__(self) -> None:
        """建立测试专用的内存数据库、会话工厂和当前模型表结构。

        共享连接池使同一测试环境不同会话看到同一内存数据，不连接应用默认数据库。
        """
        self.engine: Engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.sessions = sessionmaker(
            bind=self.engine,
            autoflush=False,
            autocommit=False,
        )
        Base.metadata.create_all(bind=self.engine)

    def dependency(self):
        """向测试接口提供数据库会话，并在请求结束后关闭。

        用 yield 与测试应用依赖机制衔接，不自动提交业务写入。
        """
        db = self.sessions()
        try:
            yield db
        finally:
            db.close()

    def close(self) -> None:
        """删除测试环境中的全部模型表并释放数据库引擎。

        只针对本测试环境，供测试结束时清理资源。
        """
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()


class ApiHarness(DatabaseHarness):
    """In-memory database plus a FastAPI test client."""

    def __init__(self, router: APIRouter) -> None:
        """在内存数据库上创建仅注册指定接口的测试应用。

        覆盖真实数据库依赖，并提供本地测试客户端，不启动外部网页服务器。
        """
        super().__init__()
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.dependency_overrides[get_db] = self.dependency
        self.client = TestClient(self.app)

    def token(self, username: str = "student", password: str = "student123") -> str:
        """通过真实登录接口获取指定测试账户的凭证。

        默认使用预先准备的演示账户；响应缺少凭证时直接报错，避免伪造登录成功。
        """
        response = self.client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )
        return response.json()["accessToken"]

    @staticmethod
    def auth(token: str) -> dict[str, str]:
        """把测试凭证转换成请求头字典。

        仅包装文本，不校验签名或用户权限。
        """
        return {"Authorization": f"Bearer {token}"}


class FakeMemoryStore:
    def __init__(self, history: list[AiMessage] | None = None) -> None:
        """复制给定历史并建立内存中的四维状态字典。

        模拟缓存只供测试使用，不连接 Redis。
        """
        self.history = list(history or [])
        self.cbt_states: dict[str, dict] = {}

    def load_recent(self, session_public_id: str) -> list[AiMessage]:
        """返回模拟历史的列表副本。

        此简化替身不按会话编号隔离历史，测试应在需要的范围内创建独立实例。
        """
        return list(self.history)

    def messages_from_rows(self, rows) -> list[AiMessage]:
        """为通用测试替身提供空的数据库消息转换结果。

        故意不还原传入记录，让需要验证数据库恢复的测试自行提供更具体的替身。
        """
        return []

    def replace(self, session_public_id: str, messages: list[AiMessage]) -> None:
        """把模拟历史替换成给定消息的列表副本。

        不执行容量裁剪、过期或外部存储操作。
        """
        self.history = list(messages)

    def append(self, session_public_id: str, role: str, content: str) -> None:
        """将一条指定角色和内容的消息追加到模拟历史。

        只记录输入，便于测试观察聊天保存后的缓存更新。
        """
        self.history.append(AiMessage(role=role, content=content))

    def save_cbt_state(self, session_public_id: str, state: dict) -> None:
        """按会话编号保存四维状态的浅复制。

        顶层字典独立，嵌套值仍遵循浅复制规则。
        """
        self.cbt_states[session_public_id] = dict(state)

    def load_cbt_state(self, session_public_id: str) -> dict:
        """读取指定会话状态的浅复制，未保存时返回空字典。

        避免测试调用方直接修改存储字典的顶层字段。
        """
        return dict(self.cbt_states.get(session_public_id, {}))


class FakeKnowledgeStore:
    def __init__(self, results: list | None = None) -> None:
        """复制预设检索结果，准备不访问外部索引的知识替身。

        初始化后所有检索均使用这组固定结果。
        """
        self.results = list(results or [])

    def retrieve(self, query: str, top_k: int | None = None):
        """返回预设检索结果的列表副本。

        不根据 query 或 top_k 真正搜索和截断，只用于隔离对话流程测试。
        """
        return list(self.results)


def build_runtime(
    runtime_type: type[AgentRuntime],
    *,
    db=None,
    settings: Settings | None = None,
    memory=None,
    knowledge=None,
    assessment=None,
) -> AgentRuntime:
    """为测试创建使用模拟模型及可替换依赖的对话执行器。

    runtime_type 选择实现，memory、knowledge、assessment 可覆盖默认替身；默认关闭外部向量检索。
    """
    settings = settings or Settings(
        ai_provider="mock",
        langgraph_checkpoint_backend="memory",
        knowledge_vector_enabled=False,
    )
    ai = AiClient(settings)
    dependencies = AgentRuntimeDependencies(
        ai=ai,
        memory=memory or FakeMemoryStore(),
        knowledge=knowledge or FakeKnowledgeStore(),
        assessment=assessment or PsychologicalAssessmentService(ai),
    )
    return runtime_type(db, settings, dependencies)
