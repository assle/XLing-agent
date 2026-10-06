"""Tests for AiClient async interface with the mock provider.

Verifies sync/async completion parity and streaming without external services.

Run: python -m pytest tests/test_ai_async.py
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.config import Settings
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient


@pytest.mark.parametrize("usage", [None, {"completion_tokens_details": None}])
def test_optional_remote_metadata_does_not_interrupt_valid_reply(usage):
    client = AiClient(_mock_settings())
    reply = client._openai_content({"model": "test-alias", "usage": usage,
                                   "choices": [{"finish_reason": "stop", "message": {"content": "valid reply"}}]})
    assert reply == "valid reply"
    assert client.last_completion_metadata["finishReason"] == "stop"
    assert client.last_completion_metadata["reasoningTokens"] is None


@pytest.mark.parametrize("reason", ["stop", "length"])
def test_local_completion_preserves_end_reason_for_comparison_gate(reason):
    client = AiClient(_mock_settings())
    assert client._ollama_content({"model": "local-tag", "done_reason": reason, "eval_count": 20,
                                   "message": {"content": "some output"}}) == "some output"
    assert client.last_completion_metadata["finishReason"] == reason


def _mock_settings() -> Settings:
    """创建使用模拟模型的配置。

    让同步与异步接口比较不依赖真实网络或模型文件。
    """
    return Settings(ai_provider="mock")


def _intent_messages() -> list[AiMessage]:
    """构造普通编程问题的消息分流请求。

    系统要求限定标签，供同步和异步接口使用完全相同输入。
    """
    return [
        AiMessage(role="system", content="你是一个用户意图分类器，只输出 CHAT、CONSULT、RISK 之一。"),
        AiMessage(role="user", content="最近上下文：\n无\n\n当前输入：\n帮我写一段 Python 代码"),
    ]


# ---------------------------------------------------------------------------
# 模拟模式下同步与异步完整回复的一致性。
# ---------------------------------------------------------------------------

def test_acomplete_matches_complete_intent():
    """对同一分流请求分别调用同步与异步完整回复入口。

    检查输出一致并包含日常对话标签。
    """
    client = AiClient(_mock_settings())
    messages = _intent_messages()
    sync = client.complete(messages)
    async_result = asyncio.run(client.acomplete(messages))
    assert sync == async_result
    assert "CHAT" in async_result


def test_acomplete_returns_string():
    """运行异步完整回复入口。

    检查返回非空文本，不把协程对象或空值当作模型结果。
    """
    client = AiClient(_mock_settings())
    result = asyncio.run(client.acomplete(_intent_messages()))
    assert isinstance(result, str)
    assert len(result) > 0


def test_acomplete_risk_keyword():
    """向异步分流请求传入明确高风险表达。

    检查模拟输出进入风险类别。
    """
    client = AiClient(_mock_settings())
    messages = [
        AiMessage(role="system", content="你是一个用户意图分类器，只输出 CHAT、CONSULT、RISK 之一。"),
        AiMessage(role="user", content="我不想活了"),
    ]
    result = asyncio.run(client.acomplete(messages))
    assert "RISK" in result


# ---------------------------------------------------------------------------
# 流式接口产出文本片段的基本检查。
# ---------------------------------------------------------------------------

def test_stream_yields_tokens():
    """完整消费模拟流式回复。

    检查至少产出一个片段且每个片段都是字符串。
    """
    client = AiClient(_mock_settings())

    async def collect():
        """在异步上下文中收集模型流的全部片段。

        返回列表给同步测试执行断言。
        """
        return [token async for token in client.stream(_intent_messages())]

    tokens = asyncio.run(collect())
    assert len(tokens) > 0
    assert all(isinstance(t, str) for t in tokens)


# ---------------------------------------------------------------------------
# 本组测试结束。
# ---------------------------------------------------------------------------
