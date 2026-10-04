"""Tests for issue 08: CBT structured questioning (4 dimensions).

Covers:
  - CBTState properties (is_complete, next_dimension, completed_count)
  - Heuristic extraction (keyword-based)
  - Multi-dimension extraction in single response
  - No overwriting of existing dimensions
  - Next question generation
  - Schema validation failure -> heuristic fallback
  - Completion state

Run: python -m pytest tests/test_cbt.py
"""
from __future__ import annotations

import json

from app.services.cbt import CBTService, CBTState

# ---------------------------------------------------------------------------
# 四维状态的完成标志、数量和下一缺失方面。
# ---------------------------------------------------------------------------

def test_empty_state_not_complete():
    """构造没有四维内容的状态。

    检查完成数为零，下一问从触发事件开始。
    """
    state = CBTState()
    assert state.is_complete is False
    assert state.next_dimension == "trigger_event"
    assert state.completed_count == 0


def test_partial_state():
    """只填写触发事件。

    检查完成数为一，下一缺失方面为想法。
    """
    state = CBTState(trigger_event="考试压力大")
    assert state.is_complete is False
    assert state.next_dimension == "thoughts"
    assert state.completed_count == 1


def test_complete_state():
    """填写四个方面的内容。

    检查完成标志为真、数量为四且没有下一缺失方面。
    """
    state = CBTState(
        trigger_event="考研冲刺", thoughts="担心考不上",
        body_reactions="失眠心悸", behavior="拖延逃避",
    )
    assert state.is_complete is True
    assert state.next_dimension is None
    assert state.completed_count == 4


def test_state_serialization():
    """导出带暂停标志的状态再恢复。

    检查原内容和暂停标志保留，完成状态仍按内容计算。
    """
    state = CBTState(trigger_event="test", paused=True)
    d = state.to_dict()
    assert d["trigger_event"] == "test"
    assert d["paused"] is True
    assert d["is_complete"] is False

    restored = CBTState.from_dict(d)
    assert restored.trigger_event == "test"
    assert restored.paused is True


# ---------------------------------------------------------------------------
# 无模型时的关键词提取。
# ---------------------------------------------------------------------------

def test_heuristic_extracts_trigger_event():
    """在无模型条件下输入与考试事件相关的表达。

    检查关键词规则提取事件，同时没有虚构想法字段。
    """
    svc = CBTService()
    state = svc.extract_dimensions("最近考研复习压力很大，时间不够用", CBTState())
    assert state.trigger_event is not None
    assert state.thoughts is None  # 本句未覆盖该方面。


def test_heuristic_extracts_thoughts():
    """输入含觉得和担心的表达。

    检查规则补充想法方面。
    """
    svc = CBTService()
    state = svc.extract_dimensions("我觉得自己肯定考不上，很担心", CBTState())
    assert state.thoughts is not None


def test_heuristic_extracts_body_reactions():
    """输入失眠、心跳和胸闷相关表达。

    检查规则填入身体反应方面。
    """
    svc = CBTService()
    state = svc.extract_dimensions("最近总是失眠，心跳很快，胸闷", CBTState())
    assert state.body_reactions is not None


def test_heuristic_extracts_behavior():
    """输入逃避、刷手机和拖延表达。

    检查规则补充行为方面。
    """
    svc = CBTService()
    state = svc.extract_dimensions("我开始逃避复习，一直刷手机拖延", CBTState())
    assert state.behavior is not None


def test_heuristic_multi_dimension():
    """一次输入同时包含四类关键词的表达。

    检查四方面均得到内容并达到完成状态。
    """
    svc = CBTService()
    # 一句话同时提供多个方面的信息。
    state = svc.extract_dimensions(
        "考研复习压力太大，我觉得自己考不上，最近失眠心悸，开始拖延逃避", CBTState()
    )
    assert state.trigger_event is not None
    assert state.thoughts is not None
    assert state.body_reactions is not None
    assert state.behavior is not None
    assert state.is_complete is True


# ---------------------------------------------------------------------------
# 已有内容不被后续提取覆盖。
# ---------------------------------------------------------------------------

def test_no_overwrite_existing_dimension():
    """已有触发事件时再输入另一事件表达。

    检查规则保留最初内容，不覆盖已收集信息。
    """
    svc = CBTService()
    current = CBTState(trigger_event="original event")
    state = svc.extract_dimensions("考研复习压力很大", current)
    assert state.trigger_event == "original event"  # 保留原有内容。


# ---------------------------------------------------------------------------
# 根据缺失方面选择下一问。
# ---------------------------------------------------------------------------

def test_next_question_for_trigger_event():
    """对空状态获取下一问。

    检查文字指向发生的事情或情境。
    """
    svc = CBTService()
    state = CBTState()
    q = svc.get_next_question(state)
    assert "什么事" in q or "情境" in q


def test_next_question_for_thoughts():
    """只填写触发事件后请求下一问。

    检查开始了解用户想法。
    """
    svc = CBTService()
    state = CBTState(trigger_event="test")
    q = svc.get_next_question(state)
    assert "想" in q


def test_next_question_complete():
    """四个方面完整后获取下一问。

    检查返回转入行动计划的说明。
    """
    svc = CBTService()
    state = CBTState(
        trigger_event="e", thoughts="t", body_reactions="b", behavior="beh"
    )
    q = svc.get_next_question(state)
    assert "行动计划" in q


# ---------------------------------------------------------------------------
# 用预设模型输出验证提取和合并。
# ---------------------------------------------------------------------------

class MockAiClient:
    def __init__(self, response: str = ""):
        """保存预设的四维提取文本。

        让测试能够模拟合法结构、缺失信息和格式错误。
        """
        self._response = response

    def complete(self, messages):
        """直接返回预设文本。

        不会根据消息真正提取信息，也不访问模型服务。
        """
        return self._response


def test_llm_extraction_success():
    """让模型替身只返回触发事件。

    检查内容准确写入，未提供的方面保持为空。
    """
    response = json.dumps({
        "trigger_event": "考研冲刺阶段时间不够",
        "thoughts": None,
        "body_reactions": None,
        "behavior": None,
    })
    svc = CBTService(MockAiClient(response))
    state = svc.extract_dimensions("考研时间不够", CBTState())
    assert state.trigger_event == "考研冲刺阶段时间不够"
    assert state.thoughts is None


def test_llm_extraction_multi_dimension():
    """让替身一次返回四个方面。

    检查合并后状态完整。
    """
    response = json.dumps({
        "trigger_event": "考研压力大",
        "thoughts": "担心考不上",
        "body_reactions": "失眠",
        "behavior": "拖延",
    })
    svc = CBTService(MockAiClient(response))
    state = svc.extract_dimensions("all dimensions", CBTState())
    assert state.is_complete is True


def test_llm_extraction_invalid_json_falls_back():
    """模型返回无法解析的文本，但用户表达包含事件关键词。

    检查自动使用规则提取仍能补充事件。
    """
    svc = CBTService(MockAiClient("not json at all"))
    state = svc.extract_dimensions("考研复习压力很大", CBTState())
    # 模型格式错误时应转入关键词提取。
    assert state.trigger_event is not None


def test_llm_extraction_merge_with_existing():
    """已有事件状态下让模型返回想法。

    检查保留原事件并补充新想法。
    """
    response = json.dumps({
        "trigger_event": None,
        "thoughts": "担心考不上",
        "body_reactions": None,
        "behavior": None,
    })
    svc = CBTService(MockAiClient(response))
    current = CBTState(trigger_event="original")
    state = svc.extract_dimensions("担心考不上", current)
    assert state.trigger_event == "original"  # 保留已有内容。
    assert state.thoughts == "担心考不上"  # 补充新内容。


# ---------------------------------------------------------------------------
# 分多轮逐步完成四维追问。
# ---------------------------------------------------------------------------

def test_sequential_flow_completes():
    """按事件、想法、身体反应和行为分四次输入。

    逐步核对下一缺失方面，最终形成完整状态。
    """
    svc = CBTService()
    state = CBTState()

    # 第一轮：了解触发事件。
    state = svc.extract_dimensions("最近考研复习压力很大", state)
    assert state.trigger_event is not None
    assert state.next_dimension == "thoughts"

    # 第二轮：了解相关想法。
    state = svc.extract_dimensions("我觉得自己肯定考不上", state)
    assert state.thoughts is not None
    assert state.next_dimension == "body_reactions"

    # 第三轮：了解身体反应。
    state = svc.extract_dimensions("最近总是失眠，心跳加速", state)
    assert state.body_reactions is not None
    assert state.next_dimension == "behavior"

    # 第四轮：了解行为。
    state = svc.extract_dimensions("开始逃避复习，一直拖延", state)
    assert state.behavior is not None
    assert state.is_complete is True


# ---------------------------------------------------------------------------
# 本组测试结束。
# ---------------------------------------------------------------------------
