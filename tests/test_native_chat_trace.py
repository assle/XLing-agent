"""Public chat/review flows using native SQLite checkpoints and observable traces."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from starlette.testclient import TestClient

from app.api import account, admin, support
from app.api.routes import router
from app.core import diagnostics, security
from app.core.config import Settings
from app.models.entities import UserAccount
from app.services.memory import RedisShortTermMemoryStore


class _Redis:
    """Replace only the external cache connection; use the production memory adapter."""

    def __init__(self):
        self.lists = {}
        self.values = {}

    def lrange(self, key, start, end):
        return self.lists.get(key, [])[start:None if end == -1 else end + 1]

    def rpush(self, key, *values):
        self.lists.setdefault(key, []).extend(values)

    def ltrim(self, key, start, end):
        self.lists[key] = self.lrange(key, start, end)

    def expire(self, key, seconds):
        return True

    def pipeline(self):
        return self

    def delete(self, key):
        self.lists.pop(key, None)
        self.values.pop(key, None)

    def execute(self):
        return []

    def set(self, key, value, ex=None):
        self.values[key] = value

    def get(self, key):
        return self.values.get(key)


@pytest.fixture
def native_api(api_harness_factory, tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None, ai_provider="mock", knowledge_vector_enabled=False,
        langgraph_checkpoint_backend="async_sqlite",
        langgraph_checkpoint_path=str(tmp_path / "checkpoints.db"),
        diagnostic_log_dir=str(tmp_path / "logs"),
        tool_queue_enabled=True, bcrypt_rounds=4,
        jwt_secret_key="native-trace-test-signing-key-at-least-thirty-two-bytes",
    )
    for module in (account, admin, support, security):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    cache = _Redis()
    monkeypatch.setattr(RedisShortTermMemoryStore, "_connect", lambda self: cache)
    harness = api_harness_factory(router)
    db = harness.sessions()
    try:
        user = UserAccount(username="trace-user", display_name="User", password_hash=security.hash_password("test"))
        user.roles = {"ROLE_USER"}
        reviewer = UserAccount(username="trace-reviewer", display_name="Reviewer", password_hash=security.hash_password("test"))
        reviewer.roles = {"ROLE_USER", "ROLE_ADMIN"}
        db.add_all([user, reviewer])
        db.commit()
    finally:
        db.close()
    user_headers = harness.auth(harness.token("trace-user", "test"))
    reviewer_headers = harness.auth(harness.token("trace-reviewer", "test"))
    diagnostics.configure(settings)
    try:
        yield harness, settings, user_headers, reviewer_headers
    finally:
        harness.client.close()
        diagnostics.shutdown()


def _events(response):
    assert response.status_code == 200, response.text
    events = []
    for block in response.text.strip().split("\n\n"):
        lines = block.splitlines()
        name = next(line[7:] for line in lines if line.startswith("event: "))
        data = json.loads(next(line[6:] for line in lines if line.startswith("data: ")))
        events.append((name, data))
    return events


def _records(settings):
    from pathlib import Path

    return [json.loads(line) for line in (Path(settings.diagnostic_log_dir) / "execution.jsonl").read_text().splitlines()]


def test_daily_http_turns_persist_messages_and_have_independent_complete_traces(native_api, capsys):
    harness, settings, user_headers, _ = native_api
    marker = "PRIVATE-SYNTHETIC-CHAT-CONTENT"
    first = _events(harness.client.post(
        "/api/chat/stream", json={"message": f"Python 怎么读取 JSON？{marker}"}, headers=user_headers,
    ))
    session_id = next(data["sessionId"] for name, data in first if name == "meta")
    second = _events(harness.client.post(
        "/api/chat/stream", json={"message": "Python 怎么写入 JSON？", "sessionId": session_id}, headers=user_headers,
    ))
    assert [name for name, _ in first][0] == "meta"
    assert [name for name, _ in first][-1] == "done"
    assert "token" in [name for name, _ in second]
    conversation = harness.client.get(f"/api/sessions/{session_id}", headers=user_headers).json()
    assert [row["role"] for row in conversation["messages"]] == ["USER", "ASSISTANT", "USER", "ASSISTANT"]
    assert marker in conversation["messages"][0]["content"]

    records = _records(settings)
    starts = [row for row in records if row["event"] == "chat.request.start"]
    assert len(starts) == 2
    assert len({row["run_id"] for row in starts}) == 2
    for start in starts:
        execution = [row for row in records if row["run_id"] == start["run_id"]]
        names = {row["event"] for row in execution}
        assert {
            "chat.prepare.end", "graph.completed", "graph.run.end", "runtime.close.end",
            "chat.stream.end", "chat.save.end", "support_turn.persisted", "chat.request.end",
        } <= names
        assert execution[-1]["thread_id"] == session_id
        assert execution[-1]["duration_ms"] >= 0
    terminal = capsys.readouterr()
    assert marker not in json.dumps(records) + terminal.out + terminal.err


def test_paused_http_turn_is_protected_and_review_reconstruction_links_its_original_run(native_api, capsys):
    harness, settings, user_headers, reviewer_headers = native_api
    marker = "PRIVATE-SYNTHETIC-HIGH-RISK-CONTENT"
    paused = _events(harness.client.post(
        "/api/chat/stream", json={"message": f"我想伤害自己 {marker}"}, headers=user_headers,
    ))
    session_id = next(data["sessionId"] for name, data in paused if name == "meta")
    assert [name for name, _ in paused] == ["meta", "pending_review", "done"]
    waiting = harness.client.get(f"/api/sessions/{session_id}", headers=user_headers).json()
    assert waiting["pendingReview"] is True
    pending = harness.client.get("/api/admin/reviews", headers=reviewer_headers).json()
    assert len(pending) == 1
    review_id = pending[0]["reviewId"]
    original_records = _records(settings)
    original_id = next(row["run_id"] for row in original_records if row["event"] == "chat.request.start")
    original = [row for row in original_records if row["run_id"] == original_id]
    assert "node.risk_guardian_gate.interrupted" in {row["event"] for row in original}
    assert "graph.interrupted" in {row["event"] for row in original}
    assert not any(row["event"].endswith(".failed") for row in original)
    assert not any(row["event"] == "chat.stream.start" for row in original)
    saved = next(row for row in original if row["event"] == "support_turn.persisted")
    assert saved["review_id"] == review_id
    assert saved["user_message_id"] > 0
    registered = [row for row in original if row["event"] == "tool_job.registered"]
    assert {row["report_id"] for row in registered} == {saved["report_id"]}
    assert len({row["job_id"] for row in registered}) == 2

    blocked = harness.client.post(
        "/api/chat/stream", json={"sessionId": session_id, "message": "继续说"}, headers=user_headers,
    )
    assert blocked.status_code == 409
    assert harness.client.get(f"/api/sessions/{session_id}", headers=user_headers).json() == waiting
    rejection = next(row for row in _records(settings) if row["event"] == "chat.blocked")
    assert rejection["reason_code"] == "PENDING_REVIEW"
    assert not any(
        row["run_id"] == rejection["run_id"] and row["event"] == "graph.run.start"
        for row in _records(settings)
    )
    unrelated = _events(harness.client.post(
        "/api/chat/stream", json={"message": "Python 怎么排序列表？"}, headers=user_headers,
    ))
    assert unrelated[-1][0] == "done"

    # Reconstruct the HTTP client. Production chat already closed its SQLite runtime;
    # the review endpoint builds a new runtime from the saved checkpoint file.
    harness.client.close()
    harness.client = TestClient(harness.app)
    approved = harness.client.post(
        f"/api/admin/reviews/{review_id}/decision", json={"decision": "approve"}, headers=reviewer_headers,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    restored = harness.client.get(f"/api/sessions/{session_id}", headers=user_headers).json()
    assert restored["pendingReview"] is False
    assert len(restored["messages"]) == 3
    assert restored["messages"][-1]["content"].startswith(approved.json()["responsePreview"])
    continued = _events(harness.client.post(
        "/api/chat/stream", json={"sessionId": session_id, "message": "Python 怎么排序？"}, headers=user_headers,
    ))
    assert continued[-1][0] == "done"

    records = _records(settings)
    review_start = next(row for row in records if row["event"] == "review.request.start")
    assert review_start["run_id"] != original_id
    assert review_start["review_id"] == review_id
    review = [row for row in records if row["run_id"] == review_start["run_id"]]
    assert {
        "graph.resume.end", "review.restore.end", "review.stream.end", "review.save.end", "review.request.end",
    } <= {row["event"] for row in review}
    assert review[-1]["origin_run_id"] == original_id
    assert next(row for row in review if row["event"] == "review.waited")["wait_ms"] >= 0
    assert review[-1]["duration_ms"] >= 0
    terminal = capsys.readouterr()
    assert marker not in json.dumps(records) + terminal.out + terminal.err


def test_http_stream_failure_keeps_completed_stages_and_excludes_provider_error(native_api, capsys, monkeypatch):
    harness, settings, user_headers, _ = native_api
    marker = "PRIVATE-SYNTHETIC-PROVIDER-ERROR"
    settings.ai_provider = "openai"
    settings.openai_api_key = "synthetic-test-key"
    original_client = httpx.AsyncClient

    class FailingResponse(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            raise RuntimeError(marker)

    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=FailingResponse()))
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: original_client(*args, transport=transport, **kwargs))
    events = _events(harness.client.post(
        "/api/chat/stream", json={"message": "Python 怎么读取 JSON？"}, headers=user_headers,
    ))
    assert events[-1][0] == "error"
    assert events[-1][1]["code"] == "CHAT_FAILED"
    assert "done" not in [name for name, _ in events]
    assert marker not in json.dumps(events)

    sessions = harness.client.get("/api/sessions", headers=user_headers).json()
    assert len(sessions) == 1
    conversation = harness.client.get(f"/api/sessions/{sessions[0]['sessionId']}", headers=user_headers).json()
    assert [row["role"] for row in conversation["messages"]] == ["USER"]
    records = _records(settings)
    run_id = next(row["run_id"] for row in records if row["event"] == "chat.request.start")
    execution = [row for row in records if row["run_id"] == run_id]
    names = {row["event"] for row in execution}
    assert {"chat.prepare.end", "graph.completed", "support_turn.persisted", "chat.stream.failed", "chat.request.failed"} <= names
    assert "chat.save.end" not in names
    stream_failure = next(row for row in execution if row["event"] == "chat.stream.failed")
    assert stream_failure["error_type"] == "RuntimeError"
    assert stream_failure["frames"]
    assert stream_failure["duration_ms"] >= 0
    terminal = capsys.readouterr()
    assert marker not in json.dumps(records) + terminal.out + terminal.err


@pytest.mark.parametrize("signal,code,word", [
    (httpx.ReadTimeout, "MODEL_TIMEOUT", "超时"),
    (httpx.ConnectError, "MODEL_UNAVAILABLE", "模型服务"),
])
def test_provider_failure_has_readable_error_without_saving_partial_reply(native_api, monkeypatch, signal, code, word):
    from app.services.ai import AiClient

    harness, _, user_headers, _ = native_api

    async def failing_stream(self, messages):
        yield "unfinished"
        raise signal("PRIVATE-ERROR-MARKER")

    monkeypatch.setattr(AiClient, "stream", failing_stream)
    events = _events(harness.client.post(
        "/api/chat/stream", json={"message": "Python 怎么读取 JSON？"}, headers=user_headers,
    ))
    assert events[-1][0] == "error"
    assert events[-1][1]["code"] == code
    assert word in events[-1][1]["message"]
    assert "PRIVATE-ERROR-MARKER" not in json.dumps(events)
    session_id = next(data["sessionId"] for name, data in events if name == "meta")
    saved = harness.client.get(f"/api/sessions/{session_id}", headers=user_headers).json()
    assert [row["role"] for row in saved["messages"]] == ["USER"]


@pytest.mark.parametrize("pending", [False, True])
def test_handled_dispatch_failure_never_exposes_tool_details(native_api, monkeypatch, pending):
    import app.services.chat as chat_module
    from app.services.report_dispatch import ReportDispatchError

    harness, settings, user_headers, _ = native_api
    marker = "PRIVATE-TOOL-RESULT-AND-ERROR"

    class FailingDispatcher:
        async def dispatch(self, report_id, risk_level):
            raise ReportDispatchError(marker)

    monkeypatch.setattr(chat_module, "create_report_dispatcher", lambda db, config: FailingDispatcher())
    text = "我有伤害自己的念头" if pending else "我最近焦虑失眠，担心工作安排"
    events = _events(harness.client.post("/api/chat/stream", json={"message": text}, headers=user_headers))
    errors = [data for name, data in events if name == "error"]
    assert errors and "后续处理暂未完成" in errors[0]["message"]
    assert marker not in json.dumps(events)
    assert pending == any(name == "pending_review" for name, _ in events)
    assert any(row.get("reason_code") == "report_dispatch_failed" for row in _records(settings))


@pytest.mark.parametrize("tokens", [[], ["", " \n"]])
def test_empty_model_stream_reports_error_and_keeps_only_user_message(native_api, monkeypatch, tokens):
    from app.services.ai import AiClient

    harness, settings, user_headers, _ = native_api

    async def empty_stream(self, messages):
        for token in tokens:
            yield token

    monkeypatch.setattr(AiClient, "stream", empty_stream)
    events = _events(harness.client.post(
        "/api/chat/stream", json={"message": "Python 怎么读取 JSON？"}, headers=user_headers,
    ))
    names = [name for name, _ in events]
    assert "error" in names
    assert "done" not in names
    assert events[-1][1]["message"]
    session_id = next(data["sessionId"] for name, data in events if name == "meta")
    saved = harness.client.get(f"/api/sessions/{session_id}", headers=user_headers).json()
    assert [row["role"] for row in saved["messages"]] == ["USER"]
    assert any(row.get("reason_code") == "empty_model_response" for row in _records(settings))


def test_asgi_disconnect_closes_streams_in_the_request_context_without_saving_partial_reply(native_api, capsys, monkeypatch):
    harness, settings, user_headers, _ = native_api
    errors = []
    bodies = []
    opened_in = []
    closed_in = []
    settings.ai_provider = "openai"
    settings.openai_api_key = "synthetic-test-key"
    original_client = httpx.AsyncClient

    class Response(httpx.AsyncByteStream):
        async def __aiter__(self):
            opened_in.append(asyncio.current_task())
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            await asyncio.Event().wait()

        async def aclose(self):
            closed_in.append(asyncio.current_task())

    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Response()))
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: original_client(*args, transport=transport, **kwargs))

    async def disconnected_request():
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda loop, context: errors.append(context))
        first_token = asyncio.Event()
        request_delivered = False

        async def receive():
            nonlocal request_delivered
            if not request_delivered:
                request_delivered = True
                return {"type": "http.request", "body": b'{"message":"Python question"}', "more_body": False}
            await first_token.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                bodies.append(message["body"])
                if b"event: token" in message["body"]:
                    first_token.set()
                    # Disconnect while ASGI send owns control, with both generators
                    # suspended at yield rather than awaiting the model provider.
                    await asyncio.Event().wait()

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/api/chat/stream", "raw_path": b"/api/chat/stream", "query_string": b"",
            "root_path": "", "server": ("testserver", 80), "client": ("127.0.0.1", 12345),
            "headers": [(b"content-type", b"application/json"), (b"authorization", user_headers["Authorization"].encode())],
        }
        await asyncio.wait_for(harness.app(scope, receive, send), timeout=5)
        await asyncio.sleep(0)
        await loop.shutdown_asyncgens()
        await asyncio.sleep(0)

    asyncio.run(disconnected_request())
    assert errors == []
    assert opened_in == closed_in
    assert any(b"event: token" in body for body in bodies)
    sessions = harness.client.get("/api/sessions", headers=user_headers).json()
    assert len(sessions) == 1
    conversation = harness.client.get(f"/api/sessions/{sessions[0]['sessionId']}", headers=user_headers).json()
    assert [row["role"] for row in conversation["messages"]] == ["USER"]
    records = _records(settings)
    run_id = next(row["run_id"] for row in records if row["event"] == "chat.request.start")
    cancelled = [row for row in records if row["event"].endswith(".cancelled")]
    assert {row["event"] for row in cancelled} == {"ai.stream.cancelled", "chat.stream.cancelled", "chat.request.cancelled"}
    assert {row["run_id"] for row in cancelled} == {run_id}
    assert not any(row["event"].endswith(".failed") for row in records)
    assert not any(row["event"] == "chat.save.end" for row in records)
    assert "Token" not in capsys.readouterr().err
