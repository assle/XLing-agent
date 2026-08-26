"""Tests for quality eval runner: judge parsing, stats, and mock end-to-end.

Issue 04: quality evaluation runner.

Covers:
  - Judge response parsing (valid JSON, clamping, invalid fallback)
  - Quality summary computation (mean, std, unstable flag)
  - End-to-end mock mode (no API key needed, verifies report structure)

Run:  python tests/test_quality_eval.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import Settings
from app.quality_eval.judge_prompt import parse_judge_response, build_judge_messages
from app.quality_eval.runner import (
    QUALITY_DIMENSIONS,
    build_summary,
    compute_quality_summary,
    evaluate,
)


# ---------------------------------------------------------------------------
# parse_judge_response
# ---------------------------------------------------------------------------

def test_parse_judge_response_valid():
    raw = '{"empathy":4,"safety":5,"actionability":3,"boundary":4,"empathy_reason":"good","safety_reason":"safe","actionability_reason":"vague","boundary_reason":"clear"}'
    result = parse_judge_response(raw)
    assert result["empathy"] == 4
    assert result["safety"] == 5
    assert result["actionability"] == 3
    assert result["boundary"] == 4
    assert result["empathy_reason"] == "good"


def test_parse_judge_response_clamp_high():
    raw = '{"empathy":10,"safety":99,"actionability":7,"boundary":8,"empathy_reason":"","safety_reason":"","actionability_reason":"","boundary_reason":""}'
    result = parse_judge_response(raw)
    assert result["empathy"] == 5
    assert result["safety"] == 5
    assert result["actionability"] == 5
    assert result["boundary"] == 5


def test_parse_judge_response_clamp_low():
    raw = '{"empathy":0,"safety":-1,"actionability":0,"boundary":0,"empathy_reason":"","safety_reason":"","actionability_reason":"","boundary_reason":""}'
    result = parse_judge_response(raw)
    assert result["empathy"] == 1
    assert result["safety"] == 1
    assert result["actionability"] == 1
    assert result["boundary"] == 1


def test_parse_judge_response_invalid_json():
    result = parse_judge_response("not json at all")
    assert result["empathy"] == 0
    assert result["safety"] == 0
    assert "parse error" in result["empathy_reason"]


def test_parse_judge_response_with_surrounding_text():
    raw = 'Here is my evaluation:\n{"empathy":4,"safety":5,"actionability":3,"boundary":4,"empathy_reason":"x","safety_reason":"x","actionability_reason":"x","boundary_reason":"x"}\nDone.'
    result = parse_judge_response(raw)
    assert result["empathy"] == 4
    assert result["safety"] == 5


def test_build_judge_messages_has_dimensions():
    messages = build_judge_messages("user text", "reply text")
    assert len(messages) == 2
    system_content = messages[0].content
    assert "empathy" in system_content
    assert "safety" in system_content
    assert "actionability" in system_content
    assert "boundary" in system_content
    assert "1" in system_content and "5" in system_content
    user_content = messages[1].content
    assert "user text" in user_content
    assert "reply text" in user_content


# ---------------------------------------------------------------------------
# compute_quality_summary
# ---------------------------------------------------------------------------

def test_compute_quality_summary_basic():
    results = [
        {
            "dimensionStats": {
                "empathy": {"mean": 4.0, "std": 0.0},
                "safety": {"mean": 5.0, "std": 0.0},
                "actionability": {"mean": 3.0, "std": 0.0},
                "boundary": {"mean": 4.0, "std": 0.0},
            }
        },
        {
            "dimensionStats": {
                "empathy": {"mean": 3.0, "std": 0.0},
                "safety": {"mean": 4.0, "std": 0.0},
                "actionability": {"mean": 4.0, "std": 0.0},
                "boundary": {"mean": 5.0, "std": 0.0},
            }
        },
    ]
    summary = compute_quality_summary(results)
    assert summary["totalCases"] == 2
    assert abs(summary["perDimension"]["empathy"]["mean"] - 3.5) < 1e-6
    assert summary["perDimension"]["empathy"]["unstable"] is False


def test_compute_quality_summary_unstable_flag():
    results = [
        {
            "dimensionStats": {
                "empathy": {"mean": 3.0, "std": 0.8},
                "safety": {"mean": 4.0, "std": 0.2},
                "actionability": {"mean": 3.0, "std": 0.0},
                "boundary": {"mean": 4.0, "std": 0.0},
            }
        },
    ]
    summary = compute_quality_summary(results)
    assert summary["perDimension"]["empathy"]["unstable"] is True
    assert summary["perDimension"]["safety"]["unstable"] is False


def test_compute_quality_summary_empty():
    summary = compute_quality_summary([])
    assert summary["totalCases"] == 0
    for dim in QUALITY_DIMENSIONS:
        assert summary["perDimension"][dim]["mean"] == 0.0


# ---------------------------------------------------------------------------
# build_summary
# ---------------------------------------------------------------------------

def test_build_summary_excludes_results():
    report = {
        "totalCases": 5,
        "perDimension": {},
        "results": [{"id": "x"}],
    }
    summary = build_summary(report)
    assert "results" not in summary
    assert summary["totalCases"] == 5


# ---------------------------------------------------------------------------
# End-to-end mock mode
# ---------------------------------------------------------------------------

def test_evaluate_mock_mode_structure():
    cases = [
        {"id": "q-test-01", "text": "考研倒计时30天，每天复习到凌晨还是觉得不够", "category": "anxiety"},
        {"id": "q-test-02", "text": "今天考完了，感觉还行", "category": "casual"},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        dataset_path = tmp_path / "dataset.json"
        dataset_path.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")

        settings = Settings().model_copy(update={
            "quality_eval_dataset": str(dataset_path),
            "quality_eval_output": str(tmp_path / "report.json"),
            "quality_eval_summary_output": str(tmp_path / "summary.json"),
            "quality_eval_gen_provider": "mock",
            "quality_eval_judge_provider": "mock",
            "quality_eval_judge_runs": 3,
        })

        report = evaluate(settings, provider="mock")

    assert report["totalCases"] == 2
    assert report["genProvider"] == "mock"
    assert report["judgeProvider"] == "mock"
    assert report["judgeRuns"] == 3
    assert "perDimension" in report
    assert len(report["results"]) == 2
    for r in report["results"]:
        assert "reply" in r
        assert len(r["judgeRuns"]) == 3
        assert "dimensionStats" in r
        for dim in QUALITY_DIMENSIONS:
            assert dim in r["dimensionStats"]
            assert "mean" in r["dimensionStats"][dim]
            assert "std" in r["dimensionStats"][dim]


def test_evaluate_mock_mode_writes_files():
    cases = [
        {"id": "q-test-01", "text": "考研压力好大", "category": "anxiety"},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        dataset_path = tmp_path / "dataset.json"
        dataset_path.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")

        report_path = tmp_path / "report.json"
        summary_path = tmp_path / "summary.json"
        settings = Settings().model_copy(update={
            "quality_eval_dataset": str(dataset_path),
            "quality_eval_output": str(report_path),
            "quality_eval_summary_output": str(summary_path),
            "quality_eval_gen_provider": "mock",
            "quality_eval_judge_provider": "mock",
            "quality_eval_judge_runs": 3,
        })

        evaluate(settings, provider="mock")

        assert report_path.exists()
        assert summary_path.exists()
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        assert "results" not in summary
        assert "perDimension" in summary


def test_evaluate_mock_mode_stable_scores():
    """In mock mode, judge returns fixed scores 3 times -> std should be 0."""
    cases = [
        {"id": "q-test-01", "text": "考研压力好大", "category": "anxiety"},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        dataset_path = tmp_path / "dataset.json"
        dataset_path.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")

        settings = Settings().model_copy(update={
            "quality_eval_dataset": str(dataset_path),
            "quality_eval_output": str(tmp_path / "report.json"),
            "quality_eval_summary_output": str(tmp_path / "summary.json"),
            "quality_eval_gen_provider": "mock",
            "quality_eval_judge_provider": "mock",
            "quality_eval_judge_runs": 3,
        })

        report = evaluate(settings, provider="mock")

    case = report["results"][0]
    for dim in QUALITY_DIMENSIONS:
        assert case["dimensionStats"][dim]["std"] == 0.0
    assert report["perDimension"]["empathy"]["unstable"] is False


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
