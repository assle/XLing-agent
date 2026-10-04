from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.core import diagnostics
from app.core.config import Settings
from app.services.ai import AiClient
from app.services.assessment import PsychologicalAssessmentService
from app.services.vector_store import ChromaKnowledgeStore, VectorStoreUnavailable


@pytest.fixture
def ollama_response(monkeypatch):
    async_client = httpx.AsyncClient

    def install(payload, *, headers=None):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload, headers=headers))

        def post(url, **kwargs):
            with httpx.Client(transport=transport) as client:
                return client.post(url, **kwargs)

        monkeypatch.setattr(httpx, "post", post)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: async_client(transport=transport, **kwargs))

    return install


@pytest.mark.parametrize("is_async", [False, True])
def test_truncated_classifier_output_is_traceable_and_preserves_conservative_assessment(
    tmp_path, ollama_response, capsys, caplog, is_async,
):
    ollama_response({
        "model": "PRIVATE-RESPONSE-MODEL",
        "created_at": "2026-10-03T01:00:00Z",
        "message": {"role": "assistant", "content": " \nNOT-A-LABEL\t", "thinking": "PRIVATE-REASONING"},
        "done": True,
        "done_reason": "length",
        "total_duration": 1234,
        "prompt_eval_count": 42,
        "eval_count": 6,
    }, headers={"x-private-token": "PRIVATE-HEADER"})
    settings = Settings(
        _env_file=None, ai_provider="openai", ollama_classifier_model="registered-classifier",
        ollama_base_url="https://PRIVATE-URL.example", openai_api_key="PRIVATE-KEY",
        diagnostic_log_dir=str(tmp_path),
    )
    diagnostics.configure(settings)
    try:
        service = PsychologicalAssessmentService(AiClient(settings))
        with diagnostics.execution("chat", session_id=12) as trace:
            result = (
                asyncio.run(service.aassess("PRIVATE-USER-INPUT"))
                if is_async else service.assess("PRIVATE-USER-INPUT")
            )
    finally:
        diagnostics.shutdown()
    output = (tmp_path / "execution.jsonl").read_text()
    terminal = capsys.readouterr()
    records = [json.loads(line) for line in output.splitlines()]
    event = next(row for row in records if row["event"] == "ai.classify.output")
    assert {key: value for key, value in event.items() if key != "timestamp"} == {
        "event": "ai.classify.output", "run_id": trace.run_id, "session_id": 12,
        "model": "registered-classifier", "provider": "ollama", "status": "non_label_output",
        "output_characters": 11, "generated_tokens": 6, "finish_reason": "length",
    }
    degraded = next(row for row in records if row["event"] == "assessment.degraded")
    assert degraded["run_id"] == trace.run_id
    assert degraded["reason_code"] == "unknown_classifier_label"
    assert degraded["status"] == "non_label_output"
    assert result.risk.value == "MEDIUM"
    assert result.confidence == 0.3
    for private in (
        "NOT-A-LABEL", "PRIVATE-USER-INPUT", "PRIVATE-REASONING", "PRIVATE-RESPONSE-MODEL",
        "PRIVATE-URL", "PRIVATE-KEY", "PRIVATE-HEADER",
    ):
        assert private not in output + terminal.out + terminal.err + caplog.text


@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.parametrize("content,status,characters", [
    (" \n正常\t", "label", 2),
    (" 焦虑 ", "label", 2),
    ("\t低落\n", "label", 2),
    (" 高风险 ", "label", 3),
    (" \n\t", "empty_output", 0),
])
def test_classifier_records_output_shape_without_changing_return_value(
    tmp_path, ollama_response, is_async, content, status, characters,
):
    ollama_response({"message": {"content": content}, "done": True, "done_reason": "stop", "eval_count": 0})
    settings = Settings(_env_file=None, ai_provider="ollama", diagnostic_log_dir=str(tmp_path))
    diagnostics.configure(settings)
    try:
        client = AiClient(settings)
        with diagnostics.execution("chat"):
            result = asyncio.run(client.aclassify("有点担心")) if is_async else client.classify("有点担心")
    finally:
        diagnostics.shutdown()
    records = [json.loads(line) for line in (tmp_path / "execution.jsonl").read_text().splitlines()]
    event = next(row for row in records if row["event"] == "ai.classify.output")
    assert result == content.strip()
    assert event["status"] == status
    assert event["output_characters"] == characters
    assert event["generated_tokens"] == 0
    assert event["finish_reason"] == "stop"


@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.parametrize("metadata", [
    {},
    {"eval_count": True, "done_reason": "PRIVATE-DONE-REASON"},
    {"eval_count": -1, "done_reason": {"private": "PRIVATE-DONE-REASON"}},
    {"eval_count": 6.0, "done_reason": ["PRIVATE-DONE-REASON"]},
    {"eval_count": "6", "done_reason": None},
])
def test_classifier_does_not_render_untrusted_generation_metadata(
    tmp_path, ollama_response, capsys, caplog, is_async, metadata,
):
    ollama_response({"message": {"content": "焦虑"}, "done": True, **metadata})
    settings = Settings(_env_file=None, ai_provider="ollama", diagnostic_log_dir=str(tmp_path))
    diagnostics.configure(settings)
    try:
        client = AiClient(settings)
        with diagnostics.execution("chat"):
            result = asyncio.run(client.aclassify("有点担心")) if is_async else client.classify("有点担心")
    finally:
        diagnostics.shutdown()
    output = (tmp_path / "execution.jsonl").read_text()
    terminal = capsys.readouterr()
    records = [json.loads(line) for line in output.splitlines()]
    event = next(row for row in records if row["event"] == "ai.classify.output")
    assert result == "焦虑"
    assert event["generated_tokens"] is None
    assert event["finish_reason"] == "unknown"
    assert "PRIVATE-DONE-REASON" not in output + terminal.out + terminal.err + caplog.text


@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.parametrize("payload,error_type", [
    (None, json.JSONDecodeError),
    ({}, KeyError),
    ({"message": {}}, KeyError),
    ({"message": {"content": None}}, AttributeError),
    ({"message": {"content": 6}}, AttributeError),
])
def test_malformed_classifier_response_still_raises(tmp_path, ollama_response, is_async, payload, error_type):
    ollama_response(payload)
    settings = Settings(_env_file=None, ai_provider="ollama", diagnostic_log_dir=str(tmp_path))
    diagnostics.configure(settings)
    try:
        client = AiClient(settings)
        with diagnostics.execution("chat"):
            with pytest.raises(error_type):
                if is_async:
                    asyncio.run(client.aclassify("有点担心"))
                else:
                    client.classify("有点担心")
    finally:
        diagnostics.shutdown()
    records = [json.loads(line) for line in (tmp_path / "execution.jsonl").read_text().splitlines()]
    assert not any(row["event"] == "ai.classify.output" for row in records)
    failed = next(row for row in records if row["event"] == "ai.classify.failed")
    assert failed["error_type"] == error_type.__name__


@pytest.mark.parametrize("label,status", [("  \n", "empty_output"), ("PRIVATE-CLASSIFIER-RESPONSE", "non_label_output")])
@pytest.mark.parametrize("is_async", [False, True])
def test_unknown_classifier_format_is_recorded_without_raw_content(tmp_path, label, status, is_async):
    class Classifier:
        def classify(self, text):
            return label

        async def aclassify(self, text):
            return label

    diagnostics.configure(Settings(_env_file=None, diagnostic_log_dir=str(tmp_path)))
    try:
        service = PsychologicalAssessmentService(Classifier())
        with diagnostics.execution("chat"):
            result = asyncio.run(service.aassess("有点紧张")) if is_async else service.assess("有点紧张")
    finally:
        diagnostics.shutdown()
    output = (tmp_path / "execution.jsonl").read_text()
    records = [json.loads(line) for line in output.splitlines()]
    event = next(row for row in records if row["event"] == "assessment.degraded")
    assert event["reason_code"] == "unknown_classifier_label"
    assert event["status"] == status
    assert "PRIVATE-CLASSIFIER-RESPONSE" not in output
    assert result.risk.value == "MEDIUM"


def test_embedding_http_failure_is_locatable_without_provider_body(tmp_path, monkeypatch):
    marker = "PRIVATE-PROVIDER-BODY"
    request = httpx.Request("POST", "https://embedding.example/v1/embeddings")
    response = httpx.Response(400, json={"error": {"message": marker}}, request=request)
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: response)
    store = object.__new__(ChromaKnowledgeStore)
    store.settings = Settings(_env_file=None, embedding_base_url="https://embedding.example/v1", embedding_api_key="PRIVATE-KEY")
    store.can_embed = True
    diagnostics.configure(Settings(_env_file=None, diagnostic_log_dir=str(tmp_path)))
    try:
        with diagnostics.execution("knowledge"):
            with pytest.raises(httpx.HTTPStatusError) as caught:
                store.embed_texts(["PRIVATE-KNOWLEDGE-TEXT"])
    finally:
        diagnostics.shutdown()
    assert caught.value.response is response
    output = (tmp_path / "execution.jsonl").read_text()
    records = [json.loads(line) for line in output.splitlines()]
    event = next(row for row in records if row["event"] == "knowledge.embed.degraded")
    assert event["reason_code"] == "embedding_http_status"
    assert event["status"] == 400
    assert event["error_type"] == "HTTPStatusError"
    for private in (marker, "PRIVATE-KEY", "PRIVATE-KNOWLEDGE-TEXT"):
        assert private not in output


def test_embedding_batches_preserve_text_order_when_batch_indices_restart(monkeypatch):
    batches = []

    def post(url, *, json, **kwargs):
        batch = json["input"]
        batches.append(batch)
        if len(batch) > 10:
            return httpx.Response(400, request=httpx.Request("POST", url))
        # 服务端返回乱序；每批编号均从零开始。
        rows = [{"index": index, "embedding": [float(text)]} for index, text in reversed(list(enumerate(batch)))]
        return httpx.Response(200, json={"data": rows}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    store = object.__new__(ChromaKnowledgeStore)
    store.settings = Settings(_env_file=None, embedding_api_key="test-only")
    store.can_embed = True
    result = store.embed_texts([str(index) for index in range(32)])
    assert [len(batch) for batch in batches] == [10, 10, 10, 2]
    assert result == [[float(index)] for index in range(32)]


def test_embedding_rejects_incomplete_later_batch(monkeypatch):
    calls = 0

    def post(url, *, json, **kwargs):
        nonlocal calls
        calls += 1
        rows = [{"index": index, "embedding": [1.0]} for index in range(len(json["input"]))]
        if calls == 2:
            rows.pop()
        return httpx.Response(200, json={"data": rows}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    store = object.__new__(ChromaKnowledgeStore)
    store.settings = Settings(_env_file=None, embedding_api_key="test-only")
    store.can_embed = True
    with pytest.raises(VectorStoreUnavailable):
        store.embed_texts(["fragment"] * 12)
    assert calls == 2


@pytest.mark.parametrize("is_async", [False, True])
def test_classifier_does_not_override_registered_training_contract(monkeypatch, is_async):
    requests = []

    def post(url, **kwargs):
        requests.append(kwargs["json"])
        return httpx.Response(200, json={"message": {"content": " 焦虑 \n"}}, request=httpx.Request("POST", url))

    class AsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            return post(url, **kwargs)

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "AsyncClient", AsyncClient)
    settings = Settings(_env_file=None, ai_provider="openai", ollama_classifier_model="registered-classifier")
    client = AiClient(settings)
    label = asyncio.run(client.aclassify("有点紧张")) if is_async else client.classify("有点紧张")
    assert label == "焦虑"
    assert requests == [{"model": "registered-classifier", "messages": [{"role": "user", "content": "有点紧张"}], "stream": False}]
