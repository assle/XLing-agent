from __future__ import annotations

from fastapi import APIRouter, FastAPI
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.agents.runtime import AgentRuntimeDependencies, AgentRuntimeService
from app.core.config import Settings
from app.core.database import Base, get_db
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient
from app.services.assessment import PsychologicalAssessmentService


class DatabaseHarness:
    """One reusable in-memory database setup for tests."""

    def __init__(self) -> None:
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
        db = self.sessions()
        try:
            yield db
        finally:
            db.close()

    def close(self) -> None:
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()


class ApiHarness(DatabaseHarness):
    """In-memory database plus a FastAPI test client."""

    def __init__(self, router: APIRouter) -> None:
        super().__init__()
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.dependency_overrides[get_db] = self.dependency
        self.client = TestClient(self.app)

    def token(self, username: str = "student", password: str = "student123") -> str:
        response = self.client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )
        return response.json()["accessToken"]

    @staticmethod
    def auth(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}


class FakeMemoryStore:
    def __init__(self, history: list[AiMessage] | None = None) -> None:
        self.history = list(history or [])
        self.cbt_states: dict[str, dict] = {}

    def load_recent(self, session_public_id: str) -> list[AiMessage]:
        return list(self.history)

    def messages_from_rows(self, rows) -> list[AiMessage]:
        return []

    def replace(self, session_public_id: str, messages: list[AiMessage]) -> None:
        self.history = list(messages)

    def append(self, session_public_id: str, role: str, content: str) -> None:
        self.history.append(AiMessage(role=role, content=content))

    def save_cbt_state(self, session_public_id: str, state: dict) -> None:
        self.cbt_states[session_public_id] = dict(state)

    def load_cbt_state(self, session_public_id: str) -> dict:
        return dict(self.cbt_states.get(session_public_id, {}))


class FakeKnowledgeStore:
    def __init__(self, results: list | None = None) -> None:
        self.results = list(results or [])

    def retrieve(self, query: str, top_k: int | None = None):
        return list(self.results)


def build_runtime(
    runtime_type: type[AgentRuntimeService],
    *,
    db=None,
    settings: Settings | None = None,
    memory=None,
    knowledge=None,
    assessment=None,
) -> AgentRuntimeService:
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
