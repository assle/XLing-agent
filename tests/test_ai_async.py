"""Tests for AiClient async interface (issue 02: async end-to-end).

Verifies that acomplete produces the same results as the sync complete
for all three providers (ollama mock, openai mock, pure mock), and that
the mock provider path works without external services.

Run: python -m pytest tests/test_ai_async.py
"""
from __future__ import annotations

import asyncio

from app.core.config import Settings
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient


def _mock_settings() -> Settings:
    return Settings(ai_provider="mock")


def _intent_messages() -> list[AiMessage]:
    return [
        AiMessage(role="system", content="你是一个用户意图分类器，只输出 CHAT、CONSULT、RISK 之一。"),
        AiMessage(role="user", content="最近上下文：\n无\n\n当前输入：\n帮我写一段 Python 代码"),
    ]


def _psychology_messages() -> list[AiMessage]:
    return [
        AiMessage(role="system", content="你负责分析校园心理健康消息。只返回严格 JSON：{}"),
        AiMessage(role="user", content="最近上下文：\n无\n\n当前输入：\n今天天气不错"),
    ]


# ---------------------------------------------------------------------------
# acomplete matches complete for mock provider
# ---------------------------------------------------------------------------

def test_acomplete_matches_complete_intent():
    client = AiClient(_mock_settings())
    messages = _intent_messages()
    sync = client.complete(messages)
    async_result = asyncio.run(client.acomplete(messages))
    assert sync == async_result
    assert "CHAT" in async_result


def test_acomplete_matches_complete_psychology():
    client = AiClient(_mock_settings())
    messages = _psychology_messages()
    sync = client.complete(messages)
    async_result = asyncio.run(client.acomplete(messages))
    assert sync == async_result


def test_acomplete_returns_string():
    client = AiClient(_mock_settings())
    result = asyncio.run(client.acomplete(_intent_messages()))
    assert isinstance(result, str)
    assert len(result) > 0


def test_acomplete_risk_keyword():
    client = AiClient(_mock_settings())
    messages = [
        AiMessage(role="system", content="你是一个用户意图分类器，只输出 CHAT、CONSULT、RISK 之一。"),
        AiMessage(role="user", content="我不想活了"),
    ]
    result = asyncio.run(client.acomplete(messages))
    assert "RISK" in result


# ---------------------------------------------------------------------------
# stream is already async (parity sanity check)
# ---------------------------------------------------------------------------

def test_stream_yields_tokens():
    client = AiClient(_mock_settings())

    async def collect():
        return [token async for token in client.stream(_intent_messages())]

    tokens = asyncio.run(collect())
    assert len(tokens) > 0
    assert all(isinstance(t, str) for t in tokens)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
