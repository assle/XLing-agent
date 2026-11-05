"""Tests for the configurable online retrieval strategy.

线上默认多查询 dense 主检索（兼容配置值 multi_query_hybrid_rrf），归一化加权保留为可选；
无模型客户端或远程改写失败时降级为单查询，不让检索整体失败。

Run: python -m pytest tests/test_retrieval_strategy.py
"""
from __future__ import annotations

from app.core.config import Settings
from app.services.knowledge import KnowledgeService, SearchResult
from tests.support import DatabaseHarness


def _service(settings: Settings, ai_client=None) -> KnowledgeService:
    """构造不依赖数据库与向量库的检索服务对象，仅用于策略分派测试。"""
    service = object.__new__(KnowledgeService)
    service.settings = settings
    service.ai_client = ai_client
    service.retriever = None
    service.vector_fallback_used = False
    service._expand_best = lambda ranked, top_k: ranked
    return service


class FakeAiClient:
    """返回固定改写查询或模拟远程故障的客户端替身。"""

    def __init__(self, queries=("原问题", "改写一", "改写二"), error: bool = False):
        self._queries = queries
        self._error = error
        self.calls = 0

    def generate_sub_queries(self, query: str, n: int = 3) -> list[str]:
        """按配置返回子查询列表或抛错。"""
        self.calls += 1
        if self._error:
            raise RuntimeError("simulated sub-query outage")
        return list(self._queries)


def test_default_strategy_is_multi_query_hybrid_rrf():
    """兼容配置值保持不变，实际行为由 ADR-0026 定义为 dense 主检索。"""
    assert Settings(_env_file=None).knowledge_retrieval_strategy == "multi_query_hybrid_rrf"


def test_default_dispatch_uses_dense_primary_as_base(monkeypatch):
    """默认策略下 retrieve 经多查询入口，并以 dense 主检索作为逐查询函数。"""
    service = _service(Settings(_env_file=None))
    calls = []

    def fake_multi_query(query, top_k=None, base_retrieve=None):
        calls.append((query, top_k, base_retrieve))
        return []

    monkeypatch.setattr(service, "_retrieve_multi_query", fake_multi_query)
    assert service.retrieve("睡眠", 2) == []
    assert calls == [("睡眠", 2, service._retrieve_dense_primary)]


def test_experiment_dense_primary_uses_shared_multi_query_implementation(monkeypatch):
    """离线 dense 策略与生产默认共用同一个多查询 Implementation。"""
    service = _service(Settings(_env_file=None))
    calls = []

    def fake_multi_query(query, top_k=None, base_retrieve=None):
        calls.append((query, top_k, base_retrieve))
        return []

    monkeypatch.setattr(service, "_retrieve_multi_query", fake_multi_query)
    assert service.retrieve_experiment("睡眠", "dense-primary", 2) == []
    assert calls == [("睡眠", 2, service._retrieve_dense_primary)]


def test_normalized_fusion_remains_available(monkeypatch):
    """显式选择 normalized_fusion 时走原归一化加权路径。"""
    service = _service(Settings(_env_file=None, knowledge_retrieval_strategy="normalized_fusion"))
    monkeypatch.setattr(service, "_retrieve_normalized_fusion", lambda query, top_k: ["legacy"])
    assert service.retrieve("睡眠", 2) == ["legacy"]


def test_multi_query_without_ai_client_falls_back_to_single_query():
    """无模型客户端时不尝试改写，只用原查询执行一次底层检索。"""
    service = _service(Settings(_env_file=None))
    calls = []

    def base(query, top_k):
        calls.append(query)
        return [SearchResult(1, "s", "内容", 0.5)]

    result = service._retrieve_multi_query("睡眠", 2, base_retrieve=base)
    assert calls == ["睡眠"]
    assert len(result) == 1


def test_multi_query_sub_query_outage_degrades_to_single_query():
    """远程改写失败时降级为单查询并保留检索结果，不向上抛错。"""
    ai = FakeAiClient(error=True)
    service = _service(Settings(_env_file=None), ai_client=ai)
    calls = []

    def base(query, top_k):
        calls.append(query)
        return [SearchResult(1, "s", "内容", 0.5)]

    result = service._retrieve_multi_query("睡眠", 2, base_retrieve=base)
    assert ai.calls == 1
    assert calls == ["睡眠"]
    assert len(result) == 1


def test_multi_query_merges_duplicates_by_highest_score():
    """多条改写查询逐条检索，重复片段保留最高分并按分数排序。"""
    ai = FakeAiClient(queries=("原问题", "改写一"))
    service = _service(Settings(_env_file=None), ai_client=ai)

    def base(query, top_k):
        if query == "原问题":
            return [SearchResult(1, "s", "甲", 0.2), SearchResult(2, "s", "乙", 0.1)]
        return [SearchResult(1, "s", "甲", 0.6)]

    result = service._retrieve_multi_query("睡眠", 2, base_retrieve=base)
    assert [(row.chunk_id, row.score) for row in result] == [(1, 0.6), (2, 0.1)]


def test_multi_query_retains_original_when_rewrite_omits_it():
    """改写不包含原问题时仍先检索原查询，并去掉重复表达。"""
    ai = FakeAiClient(queries=("改写一", "改写一", "改写二"))
    service = _service(Settings(_env_file=None), ai_client=ai)
    calls = []

    def base(query, top_k):
        calls.append(query)
        return [SearchResult(len(calls), "s", "内容", 0.5)]

    service._retrieve_multi_query("原问题", 2, base_retrieve=base)
    assert calls == ["原问题", "改写一", "改写二"]


def test_multi_query_hybrid_rrf_records_stage_trace_without_changing_results():
    """诊断轨迹记录改写、dense、BM25、融合和合并结果，排序保持一致。"""
    ai = FakeAiClient(queries=("原问题", "改写一"))
    service = _service(Settings(_env_file=None), ai_client=ai)
    dense = [SearchResult(1, "a.md", "甲", 0.9)]
    sparse = [SearchResult(2, "b.md", "乙", 0.8)]
    service._retrieve_vector = lambda query, top_k: dense
    service._retrieve_bm25 = lambda query, top_k: sparse
    trace = {}
    result = service.retrieve_experiment("原问题", "hybrid-rrf", 2, trace=trace)
    assert [row.chunk_id for row in result] == [1, 2]
    assert [entry["query"] for entry in trace["queries"]] == ["原问题", "改写一"]
    assert trace["queries"][0]["dense"][0]["chunkId"] == 1
    assert trace["queries"][0]["bm25"][0]["chunkId"] == 2
    assert len(trace["queries"][0]["fused"]) == 2
    assert len(trace["merged"]) == 2


def test_dense_primary_does_not_mix_bm25_when_vector_is_available():
    """有效 dense 结果不调用 BM25，诊断轨迹明确词法通道未参与。"""
    service = _service(Settings(_env_file=None))
    service._retrieve_vector = lambda query, top_k: [SearchResult(1, "a.md", "甲", 0.9)]
    service._retrieve_bm25 = lambda query, top_k: (_ for _ in ()).throw(AssertionError("BM25 must not run"))
    trace = {}
    result = service._retrieve_dense_primary("问题", 1, trace=trace)
    assert [row.chunk_id for row in result] == [1]
    assert trace["bm25"] == []


def test_dense_primary_preserves_local_fallback_when_vector_is_empty():
    """向量不可用时仍返回本地混合检索结果。"""
    service = _service(Settings(_env_file=None))
    service._retrieve_vector = lambda query, top_k: []
    service._retrieve_hybrid = lambda query, top_k: [SearchResult(2, "b.md", "兜底", 0.5)]
    result = service._retrieve_dense_primary("问题", 1)
    assert [row.chunk_id for row in result] == [2]


def test_status_reports_strategy_label():
    """管理状态接口的主检索标识随配置策略变化。"""
    harness = DatabaseHarness()
    try:
        with harness.sessions() as db:
            default = KnowledgeService(db, Settings(_env_file=None, knowledge_vector_enabled=False))
            assert "dense primary" in default.status()["primaryRetrieval"]
            legacy = KnowledgeService(db, Settings(
                _env_file=None, knowledge_vector_enabled=False,
                knowledge_retrieval_strategy="normalized_fusion",
            ))
            assert legacy.status()["primaryRetrieval"] == "Chroma dense + BM25 normalized fusion"
    finally:
        harness.close()
