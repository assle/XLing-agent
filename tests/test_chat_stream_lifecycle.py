"""File-backed SQLite verifies transaction release on actual ASGI disconnects."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

import app.services.chat as chat_module
from app.agents.langgraph_runtime import LangGraphAgentRuntimeService
from app.api import support
from app.core.config import Settings
from app.core.database import Base
from app.models.entities import ChatMessage, ChatSession, UserAccount
from app.schemas.dtos import ChatRequest
from app.services.ai import AiClient
from app.services.memory import RedisShortTermMemoryStore


def test_disconnect_during_prepare_releases_write_transaction_and_closes_runtime(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'business.db'}",
        connect_args={"check_same_thread": False, "timeout": 0.1},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        user = UserAccount(username="disconnect-user", display_name="Test", password_hash="unused")
        user.roles = {"ROLE_USER"}
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)

    app = FastAPI()
    app.include_router(support.router)
    owned = []
    closed = []

    def dependency():
        db = sessions()
        owned.append(db)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[support.get_db] = dependency
    app.dependency_overrides[support.current_user] = lambda: user
    settings = Settings(_env_file=None, ai_provider="mock", knowledge_vector_enabled=False)
    monkeypatch.setattr(support, "get_settings", lambda: settings)

    async def disconnect():
        started = asyncio.Event()

        class WaitingRuntime:
            async def run(self, stream_user, session, model_input):
                # resolve_session has flushed an INSERT. Retain its Session so
                # garbage collection cannot accidentally release the write lock.
                started.set()
                await asyncio.Event().wait()

            async def aclose(self):
                # Resource cleanup must survive the consuming task's cancellation.
                await asyncio.sleep(0)
                closed.append(True)

        def runtime_factory(db, config):
            owned.append(db)
            return WaitingRuntime()

        monkeypatch.setattr(chat_module, "create_agent_runtime", runtime_factory)
        delivered = False

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": json.dumps({"message": "Python question"}).encode()}
            await started.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            pass

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/api/chat/stream", "raw_path": b"/api/chat/stream", "query_string": b"",
            "root_path": "", "server": ("testserver", 80), "client": ("127.0.0.1", 12345),
            "headers": [(b"content-type", b"application/json")],
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=3)

    try:
        asyncio.run(disconnect())
        write_error = None
        try:
            with sessions() as db:
                db.add(UserAccount(username="after-disconnect", display_name="Next", password_hash="unused"))
                db.commit()
        except OperationalError as exc:
            write_error = exc
        assert write_error is None, "Disconnect retained a SQLite write transaction"
        assert closed == [True]
        assert engine.pool.checkedout() == 0
        with sessions() as db:
            assert db.query(chat_module.ChatSession).count() == 0
            assert db.query(UserAccount).filter_by(username="after-disconnect").count() == 1
    finally:
        for db in owned:
            db.close()
        engine.dispose()


def _concurrent_chat_services(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'concurrent-business.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        users = [UserAccount(username=name, display_name=name, password_hash="unused") for name in ["first", "other"]]
        db.add_all(users)
        db.flush()
        db.add_all([
            ChatSession(public_id="concurrent-first", title="First", user_id=users[0].id),
            ChatSession(public_id="concurrent-other", title="Other", user_id=users[1].id),
        ])
        db.commit()
        user_ids = [user.id for user in users]
    settings = Settings(
        _env_file=None, ai_provider="mock", knowledge_vector_enabled=False,
        langgraph_checkpoint_backend="async_sqlite",
        langgraph_checkpoint_path=str(tmp_path / "concurrent-checkpoints.db"),
    )
    monkeypatch.setattr(RedisShortTermMemoryStore, "_connect", lambda self: None)
    RedisShortTermMemoryStore._fallback.clear()

    async def collect(text, sid="concurrent-first", user_id=None):
        with sessions() as db:
            user = db.get(UserAccount, user_id or user_ids[0])
            service = chat_module.ChatService(db, settings)
            chunks = [chunk async for chunk in service.stream_chat(user, ChatRequest(message=text, sessionId=sid))]
            return [json.loads(next(line[6:] for line in chunk.splitlines() if line.startswith("data: "))) for chunk in chunks]

    return engine, sessions, collect, user_ids


def test_same_thread_cannot_replace_native_snapshot_before_current_run_reads_it(tmp_path, monkeypatch):
    """Pause the real SQLite graph precisely between ainvoke and its latest-state read."""
    engine, sessions, collect, user_ids = _concurrent_chat_services(tmp_path, monkeypatch)
    original_activity = LangGraphAgentRuntimeService._record_checkpoint_activity
    original_stream = AiClient.stream
    received_user_inputs = []

    async def observed_stream(ai, messages):
        received_user_inputs.append([message.content for message in messages if message.role == "user"][-1])
        async for token in original_stream(ai, messages):
            yield token

    monkeypatch.setattr(AiClient, "stream", observed_stream)

    async def exercise():
        graph_finished = asyncio.Event()
        release = asyncio.Event()
        paused = False

        async def activity(runtime, sid):
            nonlocal paused
            if sid == "concurrent-first" and not paused:
                paused = True
                graph_finished.set()
                await release.wait()
            await original_activity(runtime, sid)

        monkeypatch.setattr(LangGraphAgentRuntimeService, "_record_checkpoint_activity", activity)
        first = asyncio.create_task(collect("解释 Python first"))
        await asyncio.wait_for(graph_finished.wait(), 3)
        try:
            blocked = await asyncio.wait_for(collect("解释 Python overlapping"), 3)
            other = await asyncio.wait_for(collect("解释 Python other", "concurrent-other", user_ids[1]), 3)
            assert blocked[-1]["type"] == "error"
            assert blocked[-1]["code"] == "SESSION_BUSY"
            assert "等待" in blocked[-1]["message"] and "重试" in blocked[-1]["message"]
            assert other[-1]["type"] == "done"
            with sessions() as db:
                assert db.query(ChatMessage).filter_by(content="解释 Python overlapping").count() == 0
        finally:
            release.set()
            completed = await asyncio.wait_for(first, 3)
        assert completed[-1]["type"] == "done"
        assert received_user_inputs == ["解释 Python other", "解释 Python first"]

    try:
        asyncio.run(exercise())
    finally:
        RedisShortTermMemoryStore._fallback.clear()
        engine.dispose()


@pytest.mark.parametrize("outcome", ["complete", "timeout", "cancel"])
def test_same_thread_stays_busy_until_stream_completion_and_releases_after_failure(tmp_path, monkeypatch, outcome):
    engine, sessions, collect, _ = _concurrent_chat_services(tmp_path, monkeypatch)
    original_stream = AiClient.stream

    async def exercise():
        streaming = asyncio.Event()
        release = asyncio.Event()

        async def controlled_stream(ai, messages):
            if messages[-1].content == "解释 Python held":
                streaming.set()
                await release.wait()
                if outcome == "timeout":
                    raise httpx.ReadTimeout("controlled timeout")
            async for token in original_stream(ai, messages):
                yield token

        monkeypatch.setattr(AiClient, "stream", controlled_stream)
        first = asyncio.create_task(collect("解释 Python held"))
        await asyncio.wait_for(streaming.wait(), 3)
        try:
            blocked = await asyncio.wait_for(collect("解释 Python blocked"), 3)
            assert blocked[-1].get("code") == "SESSION_BUSY"
            with sessions() as db:
                assert db.query(ChatMessage).filter_by(content="解释 Python blocked").count() == 0
        finally:
            if outcome == "cancel":
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
            else:
                release.set()
                await asyncio.wait_for(first, 3)
        recovered = await asyncio.wait_for(collect("解释 Python recovered"), 3)
        assert recovered[-1]["type"] == "done"
        with sessions() as db:
            assert db.query(ChatMessage).filter_by(content="解释 Python recovered").count() == 1

    try:
        asyncio.run(exercise())
    finally:
        RedisShortTermMemoryStore._fallback.clear()
        engine.dispose()
