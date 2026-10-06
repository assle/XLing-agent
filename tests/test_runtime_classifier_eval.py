import json

import httpx
import pytest

from app.core.config import Settings
from app.schemas.dtos import AiMessage
from finetune.scripts.evaluate_runtime_classifier import (
    ObservedDeepSeekClient,
    parse_json_label,
    runtime_metrics,
)


@pytest.mark.parametrize(("raw", "finish", "expected"), [
    ('{"label":"正常"}', "stop", "正常"),
    ('{"label":"高风险"}', "stop", "高风险"),
    ('{"label":"正常"}', "length", "__INVALID__"),
    ('{"label":"正常","label":"高风险"}', "stop", "__INVALID__"),
    ('{"label":"正常","reason":"extra"}', "stop", "__INVALID__"),
    ('{"label":"HIGH"}', "stop", "__INVALID__"),
    ('["正常"]', "stop", "__INVALID__"),
])
def test_remote_label_requires_finished_unique_chinese_enum(raw, finish, expected):
    assert parse_json_label(raw, finish) == expected


def test_invalid_risk_case_remains_in_metrics():
    cases = [
        {"expected": "高风险", "predicted": "__INVALID__", "strictValid": False,
         "invalidReason": "length", "latencyMs": 100},
        {"expected": "正常", "predicted": "正常", "strictValid": True,
         "invalidReason": None, "latencyMs": 20},
    ]
    result = runtime_metrics(cases)
    assert result["totalCases"] == 2
    assert result["accuracy"] == .5
    assert result["highRiskRecall"] == 0
    assert result["outputValidity"] == .5
    assert result["invalidReasons"] == {"length": 1}


def test_answer_model_shadow_preserves_json_mode_and_safe_metadata(monkeypatch):
    original_client = httpx.Client
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "model": "fixture-model", "choices": [{
                "finish_reason": "stop", "message": {
                    "content": '{"label":"正常"}', "reasoning_content": "must-not-be-retained",
                },
            }], "usage": {"prompt_tokens": 30, "completion_tokens": 5},
        })

    def client_factory(**kwargs):
        assert kwargs["trust_env"] is False
        return original_client(transport=httpx.MockTransport(handle))

    monkeypatch.setattr(httpx, "Client", client_factory)
    client = ObservedDeepSeekClient(Settings(
        _env_file=None, ai_provider="openai", openai_api_key="fixture-only-key",
        openai_base_url="https://fixture.invalid/v1", openai_model="fixture-model",
    ), thinking="disabled")
    raw = client.complete([AiMessage(role="user", content="neutral fixture")])
    assert parse_json_label(raw, client.metadata["finishReason"]) == "正常"
    assert len(requests) == 1
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert requests[0]["max_tokens"] == 2048
    assert client.metadata == {
        "generatedTokens": 5, "promptTokens": 30, "finishReason": "stop", "providerModel": "fixture-model",
    }
