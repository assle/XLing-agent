"""Tests for issue 08: CBT structured questioning (4 dimensions).

Covers:
  - CBTState properties (is_complete, next_dimension, completed_count)
  - Heuristic extraction (keyword-based)
  - Multi-dimension extraction in single response
  - No overwriting of existing dimensions
  - Next question generation
  - Schema validation failure -> heuristic fallback
  - Completion state

Run:  python tests/test_cbt.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.ai import AiClient
from app.services.cbt import CBTService, CBTState, DIMENSIONS
from app.core.config import Settings


# ---------------------------------------------------------------------------
# CBTState properties
# ---------------------------------------------------------------------------

def test_empty_state_not_complete():
    state = CBTState()
    assert state.is_complete is False
    assert state.next_dimension == "trigger_event"
    assert state.completed_count == 0


def test_partial_state():
    state = CBTState(trigger_event="考试压力大")
    assert state.is_complete is False
    assert state.next_dimension == "thoughts"
    assert state.completed_count == 1


def test_complete_state():
    state = CBTState(
        trigger_event="考研冲刺", thoughts="担心考不上",
        body_reactions="失眠心悸", behavior="拖延逃避",
    )
    assert state.is_complete is True
    assert state.next_dimension is None
    assert state.completed_count == 4


def test_state_serialization():
    state = CBTState(trigger_event="test", paused=True)
    d = state.to_dict()
    assert d["trigger_event"] == "test"
    assert d["paused"] is True
    assert d["is_complete"] is False

    restored = CBTState.from_dict(d)
    assert restored.trigger_event == "test"
    assert restored.paused is True


# ---------------------------------------------------------------------------
# Heuristic extraction
# ---------------------------------------------------------------------------

def test_heuristic_extracts_trigger_event():
    svc = CBTService()
    state = svc.extract_dimensions("最近考研复习压力很大，时间不够用", CBTState())
    assert state.trigger_event is not None
    assert state.thoughts is None  # not covered


def test_heuristic_extracts_thoughts():
    svc = CBTService()
    state = svc.extract_dimensions("我觉得自己肯定考不上，很担心", CBTState())
    assert state.thoughts is not None


def test_heuristic_extracts_body_reactions():
    svc = CBTService()
    state = svc.extract_dimensions("最近总是失眠，心跳很快，胸闷", CBTState())
    assert state.body_reactions is not None


def test_heuristic_extracts_behavior():
    svc = CBTService()
    state = svc.extract_dimensions("我开始逃避复习，一直刷手机拖延", CBTState())
    assert state.behavior is not None


def test_heuristic_multi_dimension():
    svc = CBTService()
    # Single response covering multiple dimensions
    state = svc.extract_dimensions(
        "考研复习压力太大，我觉得自己考不上，最近失眠心悸，开始拖延逃避", CBTState()
    )
    assert state.trigger_event is not None
    assert state.thoughts is not None
    assert state.body_reactions is not None
    assert state.behavior is not None
    assert state.is_complete is True


# ---------------------------------------------------------------------------
# No overwriting
# ---------------------------------------------------------------------------

def test_no_overwrite_existing_dimension():
    svc = CBTService()
    current = CBTState(trigger_event="original event")
    state = svc.extract_dimensions("考研复习压力很大", current)
    assert state.trigger_event == "original event"  # not overwritten


# ---------------------------------------------------------------------------
# Next question
# ---------------------------------------------------------------------------

def test_next_question_for_trigger_event():
    svc = CBTService()
    state = CBTState()
    q = svc.get_next_question(state)
    assert "什么事" in q or "情境" in q


def test_next_question_for_thoughts():
    svc = CBTService()
    state = CBTState(trigger_event="test")
    q = svc.get_next_question(state)
    assert "想" in q


def test_next_question_complete():
    svc = CBTService()
    state = CBTState(
        trigger_event="e", thoughts="t", body_reactions="b", behavior="beh"
    )
    q = svc.get_next_question(state)
    assert "行动计划" in q


def test_next_question_with_stage():
    svc = CBTService()
    state = CBTState()
    q = svc.get_next_question(state, exam_stage="冲刺")
    assert "冲刺" in q


# ---------------------------------------------------------------------------
# LLM extraction with mock
# ---------------------------------------------------------------------------

class MockAiClient:
    def __init__(self, response: str = ""):
        self._response = response

    def complete(self, messages):
        return self._response


def test_llm_extraction_success():
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
    svc = CBTService(MockAiClient("not json at all"))
    state = svc.extract_dimensions("考研复习压力很大", CBTState())
    # Should fall back to heuristic
    assert state.trigger_event is not None


def test_llm_extraction_merge_with_existing():
    response = json.dumps({
        "trigger_event": None,
        "thoughts": "担心考不上",
        "body_reactions": None,
        "behavior": None,
    })
    svc = CBTService(MockAiClient(response))
    current = CBTState(trigger_event="original")
    state = svc.extract_dimensions("担心考不上", current)
    assert state.trigger_event == "original"  # preserved
    assert state.thoughts == "担心考不上"  # new


# ---------------------------------------------------------------------------
# Sequential CBT flow
# ---------------------------------------------------------------------------

def test_sequential_flow_completes():
    svc = CBTService()
    state = CBTState()

    # Round 1: trigger event
    state = svc.extract_dimensions("最近考研复习压力很大", state)
    assert state.trigger_event is not None
    assert state.next_dimension == "thoughts"

    # Round 2: thoughts
    state = svc.extract_dimensions("我觉得自己肯定考不上", state)
    assert state.thoughts is not None
    assert state.next_dimension == "body_reactions"

    # Round 3: body reactions
    state = svc.extract_dimensions("最近总是失眠，心跳加速", state)
    assert state.body_reactions is not None
    assert state.next_dimension == "behavior"

    # Round 4: behavior
    state = svc.extract_dimensions("开始逃避复习，一直拖延", state)
    assert state.behavior is not None
    assert state.is_complete is True


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    passed = 0
    failed = 0
    for test in _TESTS:
        try:
            test()
            print(f"  PASS  {test.__name__}")
            passed += 1
        except AssertionError as exc:
            print(f"  FAIL  {test.__name__}: {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERROR {test.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(_TESTS)} total")
    sys.exit(1 if failed else 0)
