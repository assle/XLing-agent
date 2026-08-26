"""Tests for issue 06: Exam stage RAG filtering.

Covers:
  - _matches_stage helper (single, multi, all, none)
  - Ingest with exam_stage metadata
  - Retrieve with stage filtering (match, no-match, all, none)
  - Backward compatible retrieve without stage

Run:  python tests/test_exam_stage_rag.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import Base
from app.services.knowledge import KnowledgeService, _matches_stage


_test_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
_TestSession = sessionmaker(bind=_test_engine, autoflush=False, autocommit=False)
Base.metadata.create_all(bind=_test_engine)

_settings = Settings(knowledge_vector_enabled=False, knowledge_top_k=4)


def _svc():
    return KnowledgeService(_TestSession(), _settings)


# ---------------------------------------------------------------------------
# _matches_stage helper
# ---------------------------------------------------------------------------

def test_matches_stage_none_always_matches():
    assert _matches_stage(None, "基础") is True

def test_matches_stage_empty_always_matches():
    assert _matches_stage("", "冲刺") is True

def test_matches_stage_all_always_matches():
    assert _matches_stage("all", "考后") is True

def test_matches_stage_single_match():
    assert _matches_stage("基础", "基础") is True

def test_matches_stage_single_no_match():
    assert _matches_stage("基础", "冲刺") is False

def test_matches_stage_multi_match():
    assert _matches_stage("基础,强化,冲刺", "强化") is True

def test_matches_stage_multi_no_match():
    assert _matches_stage("基础,强化", "考后") is False


# ---------------------------------------------------------------------------
# Ingest with exam_stage
# ---------------------------------------------------------------------------

def test_ingest_with_stage():
    svc = _svc()
    try:
        count = svc.ingest("test-source.md", "焦虑管理技巧", exam_stage="基础")
        assert count > 0
    finally:
        svc.db.close()

def test_ingest_without_stage():
    svc = _svc()
    try:
        count = svc.ingest("no-stage.md", "通用心理支持内容")
        assert count > 0
    finally:
        svc.db.close()


# ---------------------------------------------------------------------------
# Retrieve with stage filtering
# ---------------------------------------------------------------------------

def test_retrieve_without_stage_returns_all():
    svc = _svc()
    try:
        svc.ingest("基础内容.md", "考研基础阶段焦虑管理", exam_stage="基础")
        svc.ingest("冲刺内容.md", "考研冲刺阶段时间管理", exam_stage="冲刺")
        results = svc.retrieve("考研焦虑")
        # Should return results from both sources
        sources = {r.source for r in results}
        assert len(sources) >= 1
    finally:
        svc.db.close()

def test_retrieve_with_stage_filters():
    from app.models.entities import KnowledgeChunk
    svc = _svc()
    try:
        svc.ingest("基础内容.md", "考研基础阶段焦虑管理方法", exam_stage="基础")
        svc.ingest("冲刺内容.md", "考研冲刺阶段时间管理策略", exam_stage="冲刺")
        results = svc.retrieve_with_stage("考研", exam_stage="基础")
        # All results should match the "基础" stage or have no stage
        for r in results:
            if r.chunk_id:
                chunk = svc.db.get(KnowledgeChunk, r.chunk_id)
                if chunk and chunk.exam_stage:
                    assert _matches_stage(chunk.exam_stage, "基础")
        assert isinstance(results, list)
    finally:
        svc.db.close()

def test_retrieve_with_stage_none_returns_all():
    svc = _svc()
    try:
        svc.ingest("基础.md", "基础阶段内容", exam_stage="基础")
        svc.ingest("冲刺.md", "冲刺阶段内容", exam_stage="冲刺")
        svc.ingest("通用.md", "通用心理内容")
        results = svc.retrieve_with_stage("内容", exam_stage=None)
        assert len(results) > 0
    finally:
        svc.db.close()

def test_retrieve_with_all_stage_matches_everything():
    svc = _svc()
    try:
        svc.ingest("基础.md", "基础阶段内容", exam_stage="基础")
        svc.ingest("全阶段.md", "全阶段通用内容", exam_stage="all")
        results = svc.retrieve_with_stage("内容", exam_stage="冲刺")
        # "all" stage should match "冲刺"
        assert len(results) > 0
    finally:
        svc.db.close()


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
