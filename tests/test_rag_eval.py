"""Tests for RAG eval runner: metrics, baseline summary, SQLite wiring.

Issue 01: SQLite eval baseline + OpenAI provider.

Covers:
  - Pure metric functions (ndcg, is_relevant, evaluate_case)
  - Report aggregation + compact baseline summary (5 metrics for cross-run compare)
  - SQLite engine + eval-settings wiring (isolated Chroma dir, no MySQL)
  - End-to-end hybrid-fallback eval (no chromadb / OPENAI_API_KEY needed)

The true OpenAI vector baseline requires OPENAI_API_KEY + chromadb; the hybrid
end-to-end test here proves the SQLite wiring, seeding, retrieval and metric
pipeline produce meaningful (non-zero) numbers.

Run: python -m pytest tests/test_rag_eval.py
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from app.services.bge_retrieval import BgeM3Retriever
from app.services.knowledge import SearchResult
from app.services.vector_store import FALLBACK_RETRIEVAL_LABEL, PRIMARY_RETRIEVAL_LABEL
from evals.config import EvalSettings
from evals.rag.runner import (
    _build_engine,
    _eval_settings,
    _retrieval_label,
    _strategy_label,
    _strategy_paths,
    build_comparison,
    build_eval_summary,
    build_retrieval_decision,
    compute_report,
    evaluate,
    evaluate_case,
    format_comparison_markdown,
    is_relevant,
    ndcg,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DATASET = REPO_ROOT / "evals" / "rag" / "xling-rag-eval.jsonl"


class FakeKnowledgeService:
    """Test double -- returns canned SearchResults, no DB/Chroma."""

    def __init__(self, results: list[SearchResult]):
        """保存预设检索结果顺序。

        让指标测试无需真实知识库或向量服务。
        """
        self._results = results

    def retrieve(self, query: str, top_k: int):  # noqa: ANN001
        """返回预设列表的前 top_k 项。

        忽略 query，只用于验证后续指标计算。
        """
        return self._results[:top_k]


# ---------------------------------------------------------------------------
# Pure metrics: ndcg
# ---------------------------------------------------------------------------

def test_ndcg_all_relevant_is_one():
    """列表中全部结果相关。

    检查归一化排序得分为一。
    """
    items = [{"relevant": True}, {"relevant": True}, {"relevant": True}]
    assert abs(ndcg(items) - 1.0) < 1e-9


def test_ndcg_none_relevant_is_zero():
    """列表中没有相关结果。

    检查排序得分为零。
    """
    items = [{"relevant": False}, {"relevant": False}]
    assert ndcg(items) == 0.0


def test_ndcg_empty_is_zero():
    """对空检索结果计算排序指标。

    检查返回零而不是除零或报错。
    """
    assert ndcg([]) == 0.0


def test_ndcg_relevant_first_beats_relevant_last():
    """比较同一相关项位于首位和末位两种列表。

    检查相关结果靠前时分数更高且不超过一。
    """
    first = ndcg([{"relevant": True}, {"relevant": False}])
    last = ndcg([{"relevant": False}, {"relevant": True}])
    assert first > last > 0.0
    assert first <= 1.0


# ---------------------------------------------------------------------------
# Pure metrics: is_relevant
# ---------------------------------------------------------------------------

def test_is_relevant_source_match():
    """候选来源直接命中期望来源。

    检查即使正文没有指定词项也认定相关。
    """
    assert is_relevant("risk-policy.md", "anything", {"risk-policy.md"}, []) is True


def test_is_relevant_term_match():
    """来源不匹配但正文含期望词项。

    检查词项命中也可判定相关。
    """
    assert is_relevant("other.md", "severe hopelessness and suicide", set(), ["suicide", "hopelessness"]) is True


def test_is_relevant_short_term_ignored():
    # terms shorter than 2 chars must not match
    """只给一个字符的期望词项。

    检查过短词项被忽略，避免过宽匹配。
    """
    assert is_relevant("other.md", "x y z", set(), ["x"]) is False


def test_is_relevant_no_match():
    """来源和正文都没有期望内容。

    检查明确判为不相关。
    """
    assert is_relevant("other.md", "unrelated content", {"risk-policy.md"}, ["nonexistent"]) is False


# ---------------------------------------------------------------------------
# evaluate_case
# ---------------------------------------------------------------------------

def test_evaluate_case_hit_at_rank_one():
    """用首位相关的固定检索结果评估一个样本。

    核对首位名次、倒数排名和命中指标，并确认精确率以请求数量为分母。
    """
    results = [
        SearchResult(1, "risk-policy.md", "HIGH risk immediate danger self-harm suicide alert", 0.9),
        SearchResult(2, "campus-mental-health.md", "breathing grounding routine", 0.5),
    ]
    case = {
        "id": "t1",
        "question": "学生想自杀",
        "expectedSources": ["risk-policy.md"],
        "expectedTerms": ["HIGH", "suicide"],
    }
    result = evaluate_case(FakeKnowledgeService(results).retrieve, case, 4)
    assert result["hit"] is True
    assert result["firstRelevantRank"] == 1
    assert result["recallAtK"] == 1.0
    assert result["reciprocalRank"] == 1.0
    assert result["precisionAtK"] == 0.25  # 1 relevant / top_k=4
    assert result["ndcgAtK"] == 1.0


def test_evaluate_case_no_hit():
    """只返回与样本无关的资料。

    检查首次名次为零，命中及排序指标都为零。
    """
    results = [SearchResult(3, "other.md", "unrelated content", 0.1)]
    case = {
        "id": "t2",
        "question": "x",
        "expectedSources": ["risk-policy.md"],
        "expectedTerms": ["nonexistent"],
    }
    result = evaluate_case(FakeKnowledgeService(results).retrieve, case, 4)
    assert result["hit"] is False
    assert result["firstRelevantRank"] == 0
    assert result["recallAtK"] == 0.0
    assert result["reciprocalRank"] == 0.0
    assert result["ndcgAtK"] == 0.0


# ---------------------------------------------------------------------------
# compute_report + build_eval_summary
# ---------------------------------------------------------------------------

def test_compute_report_aggregation():
    """汇总一例命中、一例未命中的已知指标。

    检查平均命中与倒数排名为二分之一，首次相关名次只对命中样本平均。
    """
    results = [
        {"hit": True, "firstRelevantRank": 1, "recallAtK": 1.0, "precisionAtK": 0.25,
         "reciprocalRank": 1.0, "ndcgAtK": 1.0},
        {"hit": False, "firstRelevantRank": 0, "recallAtK": 0.0, "precisionAtK": 0.0,
         "reciprocalRank": 0.0, "ndcgAtK": 0.0},
    ]
    report = compute_report(results, "dataset.json", top_k=4, retrieval_label=PRIMARY_RETRIEVAL_LABEL)
    assert report["totalCases"] == 2
    assert report["topK"] == 4
    assert report["retrieval"] == PRIMARY_RETRIEVAL_LABEL
    assert report["hitRate"] == 0.5
    assert report["recallAtK"] == 0.5
    assert report["mrr"] == 0.5
    assert report["averageFirstRelevantRank"] == 1.0  # only hits counted


def test_build_eval_summary_has_five_metrics_and_no_per_case():
    """从完整检索报告提取摘要。

    检查保留五项质量指标和路径标签，但删除逐例结果。
    """
    report = {
        "createdAt": "2026-01-01T00:00:00",
        "dataset": "d.json",
        "topK": 4,
        "retrieval": PRIMARY_RETRIEVAL_LABEL,
        "totalCases": 60,
        "recallAtK": 0.8,
        "precisionAtK": 0.5,
        "mrr": 0.7,
        "ndcgAtK": 0.6,
        "hitRate": 0.9,
        "averageFirstRelevantRank": 1.2,
        "results": [{"id": "x"}],
    }
    summary = build_eval_summary(report)
    for key in ["recallAtK", "precisionAtK", "mrr", "ndcgAtK", "hitRate"]:
        assert key in summary
    assert summary["retrieval"] == PRIMARY_RETRIEVAL_LABEL
    assert summary["totalCases"] == 60
    assert "results" not in summary  # compact, no per-case detail


def test_rag_evaluation_defaults_to_acceptance_top_five():
    """检查默认评估返回数量。

    要求前五项作为当前评估口径。
    """
    assert EvalSettings().rag_eval_top_k == 5


# ---------------------------------------------------------------------------
# SQLite engine + eval-settings wiring
# ---------------------------------------------------------------------------

def test_build_engine_is_sqlite():
    """创建内存文件数据库引擎。

    检查所用数据库类型正确，不连接默认服务器数据库。
    """
    engine = _build_engine("sqlite://")
    assert engine.dialect.name == "sqlite"


def test_build_engine_creates_parent_dir_for_file_sqlite():
    """为位于尚不存在目录的数据库创建引擎。

    检查父目录被准备好。
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "nested" / "eval.db"
        _build_engine(f"sqlite:///{db_path}")
        assert db_path.parent.exists()


def test_eval_settings_swaps_chroma_dirs_to_eval():
    """从应用设置复制评估配置。

    检查向量目录和集合切换为评估专用位置，数据库评估地址保持配置值。
    """
    base = EvalSettings()
    eval_settings = _eval_settings(base)
    assert eval_settings.chroma_persist_dir == base.rag_eval_chroma_persist_dir
    assert eval_settings.chroma_collection_name == base.rag_eval_chroma_collection_name
    # eval-only fields pass through unchanged
    assert eval_settings.rag_eval_database_url == base.rag_eval_database_url


class _FakeStore:
    def __init__(self, can_embed: bool):
        """保存向量能力是否可用的标志。

        供路径标签测试控制不同状态。
        """
        self.can_embed = can_embed


class _FakeService:
    def __init__(self, can_embed: bool):
        """把可用性标志包装在向量库替身中。

        只提供标签判断所需结构。
        """
        self.vector_store = _FakeStore(can_embed)


def test_retrieval_label_primary_when_can_embed():
    """让替身报告向量可用且没有回退标志。

    检查报告使用主检索标签。
    """
    assert _retrieval_label(_FakeService(can_embed=True)) == PRIMARY_RETRIEVAL_LABEL


def test_retrieval_label_fallback_when_disabled():
    """让替身报告向量不可用。

    检查报告标明本地备用检索。
    """
    assert _retrieval_label(_FakeService(can_embed=False)) == FALLBACK_RETRIEVAL_LABEL


# ---------------------------------------------------------------------------
# End-to-end hybrid-fallback eval (no chromadb / OPENAI_API_KEY required)
# ---------------------------------------------------------------------------

def test_evaluate_end_to_end_hybrid_fallback():
    """关闭向量服务，在临时数据库跑完整检索评估。

    检查一百个样本、版本记录及非零质量指标，并明确使用备用路径。
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        settings = EvalSettings().model_copy(update={
            "rag_eval_dataset": str(DATASET),
            "rag_eval_database_url": f"sqlite:///{tmp_path / 'eval.db'}",
            "knowledge_vector_enabled": False,  # no chromadb / key needed
            "rag_eval_output": str(tmp_path / "report.json"),
            "rag_eval_baseline_output": str(tmp_path / "baseline.json"),
        })
        report = evaluate(settings)

    assert report["totalCases"] == 100
    assert report["artifactVersion"]["datasetVersion"]
    assert report["retrieval"] == FALLBACK_RETRIEVAL_LABEL
    # meaningful numbers: not all zero, cases have hits
    assert report["hitRate"] > 0.0
    assert report["recallAtK"] > 0.0
    assert report["mrr"] > 0.0
    assert report["ndcgAtK"] > 0.0
    assert report["averageFirstRelevantRank"] > 0.0


def test_evaluate_writes_baseline_file():
    """用临时位置运行基线检索。

    检查详细报告与摘要均写入，摘要保留数量、五项指标及备用标签。
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        baseline_path = tmp_path / "baseline.json"
        report_path = tmp_path / "report.json"
        settings = EvalSettings().model_copy(update={
            "rag_eval_dataset": str(DATASET),
            "rag_eval_database_url": f"sqlite:///{tmp_path / 'eval.db'}",
            "knowledge_vector_enabled": False,
            "rag_eval_output": str(report_path),
            "rag_eval_baseline_output": str(baseline_path),
        })
        evaluate(settings)

        assert report_path.exists()
        assert baseline_path.exists()
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        for key in ["recallAtK", "precisionAtK", "mrr", "ndcgAtK", "hitRate"]:
            assert key in baseline
        assert baseline["totalCases"] == 100
        assert baseline["retrieval"] == FALLBACK_RETRIEVAL_LABEL


# ---------------------------------------------------------------------------
# Strategy helpers (issues 02-04)
# ---------------------------------------------------------------------------

def test_strategy_label_baseline():
    """取得默认方案名称。

    检查直接沿用实际检索路径标签。
    """
    assert _strategy_label("baseline", FALLBACK_RETRIEVAL_LABEL) == FALLBACK_RETRIEVAL_LABEL


def test_strategy_label_multi_query():
    """为多查询方案生成名称。

    检查同时包含多查询和底层检索路径。
    """
    label = _strategy_label("multi-query", FALLBACK_RETRIEVAL_LABEL)
    assert "multi-query" in label
    assert FALLBACK_RETRIEVAL_LABEL in label


def test_strategy_label_hybrid_rrf():
    """读取排名融合方案名称。

    检查标签说明包含多查询与融合步骤。
    """
    label = _strategy_label("hybrid-rrf", FALLBACK_RETRIEVAL_LABEL)
    assert "hybrid-RRF" in label
    assert "multi-query" in label


def test_strategy_label_llm_rerank():
    """读取模型重排方案名称。

    检查能与未重排方案明确区分。
    """
    label = _strategy_label("llm-rerank", FALLBACK_RETRIEVAL_LABEL)
    assert "LLM-rerank" in label


def test_strategy_paths_baseline():
    """查询基线报告和摘要路径。

    检查摘要使用基线专用配置。
    """
    s = EvalSettings()
    report, summary = _strategy_paths("baseline", s)
    assert summary == s.rag_eval_baseline_output


def test_strategy_paths_multi_query():
    """查询多查询方案输出位置。

    检查使用对应摘要配置而不覆盖其他方案。
    """
    s = EvalSettings()
    _, summary = _strategy_paths("multi-query", s)
    assert summary == s.rag_eval_multi_query_output


def test_strategy_paths_hybrid_rrf():
    """查询排名融合方案输出位置。

    检查摘要路径映射正确。
    """
    s = EvalSettings()
    _, summary = _strategy_paths("hybrid-rrf", s)
    assert summary == s.rag_eval_hybrid_rrf_output


def test_strategy_paths_llm_rerank():
    """查询模型重排方案输出位置。

    检查使用重排专用摘要路径。
    """
    s = EvalSettings()
    _, summary = _strategy_paths("llm-rerank", s)
    assert summary == s.rag_eval_llm_rerank_output


# ---------------------------------------------------------------------------
# RRF fusion unit test (issue 03)
# ---------------------------------------------------------------------------

def test_rrf_fuse_chunk_in_both_lists_ranks_first():
    """准备两份列表，其中一个片段同时命中两路。

    检查累计排名倒数分数让它排首位，结果仍按编号去重。
    """
    from app.services.knowledge import KnowledgeService
    list_a = [SearchResult(1, "a.md", "content a", 0.9), SearchResult(2, "b.md", "content b", 0.8)]
    list_b = [SearchResult(2, "b.md", "content b", 0.7), SearchResult(3, "c.md", "content c", 0.6)]
    fused = KnowledgeService._rrf_fuse([list_a, list_b], top_k=3, k=60)
    assert fused[0].chunk_id == 2
    assert len(fused) == 3


def test_rrf_fuse_empty_lists():
    """对两个空检索列表进行融合。

    检查结果为空。
    """
    from app.services.knowledge import KnowledgeService
    fused = KnowledgeService._rrf_fuse([[], []], top_k=4, k=60)
    assert fused == []


def test_rrf_fuse_single_list():
    """只提供一个含单条结果的列表。

    检查融合保留该片段，不产生额外候选。
    """
    from app.services.knowledge import KnowledgeService
    items = [SearchResult(1, "a.md", "content a", 0.9)]
    fused = KnowledgeService._rrf_fuse([items], top_k=4, k=60)
    assert len(fused) == 1
    assert fused[0].chunk_id == 1


# ---------------------------------------------------------------------------
# Multi-query end-to-end (issue 02)
# ---------------------------------------------------------------------------

def test_evaluate_multi_query_end_to_end():
    """在模拟模型和本地备用检索上运行多查询评估。

    检查全部样本完成、名称正确且有非零命中。
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        settings = EvalSettings().model_copy(update={
            "rag_eval_dataset": str(DATASET),
            "rag_eval_database_url": f"sqlite:///{tmp_path / 'eval.db'}",
            "knowledge_vector_enabled": False,
            "ai_provider": "mock",
            "rag_eval_ai_provider": "mock",
            "rag_eval_output": str(tmp_path / "report.json"),
            "rag_eval_baseline_output": str(tmp_path / "baseline.json"),
            "rag_eval_multi_query_output": str(tmp_path / "multi-query.json"),
        })
        report = evaluate(settings, strategy="multi-query")

    assert report["totalCases"] == 100
    assert "multi-query" in report["retrieval"]
    assert report["hitRate"] > 0.0
    assert report["recallAtK"] > 0.0


def test_evaluate_multi_query_writes_summary_file():
    """运行多查询方案并读取实际摘要文件。

    检查五项指标、案例数及方案标签完整。
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        summary_path = tmp_path / "multi-query.json"
        settings = EvalSettings().model_copy(update={
            "rag_eval_dataset": str(DATASET),
            "rag_eval_database_url": f"sqlite:///{tmp_path / 'eval.db'}",
            "knowledge_vector_enabled": False,
            "ai_provider": "mock",
            "rag_eval_ai_provider": "mock",
            "rag_eval_output": str(tmp_path / "report.json"),
            "rag_eval_baseline_output": str(tmp_path / "baseline.json"),
            "rag_eval_multi_query_output": str(summary_path),
        })
        evaluate(settings, strategy="multi-query")
        assert summary_path.exists()
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        for key in ["recallAtK", "precisionAtK", "mrr", "ndcgAtK", "hitRate"]:
            assert key in summary
        assert summary["totalCases"] == 100
        assert "multi-query" in summary["retrieval"]


# ---------------------------------------------------------------------------
# Hybrid RRF end-to-end (issue 03)
# ---------------------------------------------------------------------------

def test_evaluate_hybrid_rrf_end_to_end():
    """使用模拟查询改写运行排名融合检索。

    检查一百个样本完成且报告标明融合方案。
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        settings = EvalSettings().model_copy(update={
            "rag_eval_dataset": str(DATASET),
            "rag_eval_database_url": f"sqlite:///{tmp_path / 'eval.db'}",
            "knowledge_vector_enabled": False,
            "ai_provider": "mock",
            "rag_eval_ai_provider": "mock",
            "rag_eval_output": str(tmp_path / "report.json"),
            "rag_eval_baseline_output": str(tmp_path / "baseline.json"),
            "rag_eval_hybrid_rrf_output": str(tmp_path / "hybrid-rrf.json"),
        })
        report = evaluate(settings, strategy="hybrid-rrf")

    assert report["totalCases"] == 100
    assert "hybrid-RRF" in report["retrieval"]
    assert report["hitRate"] > 0.0


def test_evaluate_hybrid_rrf_writes_summary_file():
    """运行融合方案并检查摘要落盘。

    确认文件中的检索标签与所选方案一致。
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        summary_path = tmp_path / "hybrid-rrf.json"
        settings = EvalSettings().model_copy(update={
            "rag_eval_dataset": str(DATASET),
            "rag_eval_database_url": f"sqlite:///{tmp_path / 'eval.db'}",
            "knowledge_vector_enabled": False,
            "ai_provider": "mock",
            "rag_eval_ai_provider": "mock",
            "rag_eval_output": str(tmp_path / "report.json"),
            "rag_eval_baseline_output": str(tmp_path / "baseline.json"),
            "rag_eval_hybrid_rrf_output": str(summary_path),
        })
        evaluate(settings, strategy="hybrid-rrf")
        assert summary_path.exists()
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        assert "hybrid-RRF" in summary["retrieval"]


# ---------------------------------------------------------------------------
# LLM rerank end-to-end (issue 04)
# ---------------------------------------------------------------------------

def test_evaluate_llm_rerank_end_to_end():
    """在模拟模型条件下运行含重排的组合方案。

    检查样本数量、重排标签和有效命中。
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        settings = EvalSettings().model_copy(update={
            "rag_eval_dataset": str(DATASET),
            "rag_eval_database_url": f"sqlite:///{tmp_path / 'eval.db'}",
            "knowledge_vector_enabled": False,
            "ai_provider": "mock",
            "rag_eval_ai_provider": "mock",
            "rag_eval_output": str(tmp_path / "report.json"),
            "rag_eval_baseline_output": str(tmp_path / "baseline.json"),
            "rag_eval_llm_rerank_output": str(tmp_path / "llm-rerank.json"),
        })
        report = evaluate(settings, strategy="llm-rerank")

    assert report["totalCases"] == 100
    assert "LLM-rerank" in report["retrieval"]
    assert report["hitRate"] > 0.0


class _FakeBgeEvalBackend:
    model_name = "fake-bge-m3"
    reranker_name = "fake-bge-reranker"
    index_size_bytes = 4096

    def score(self, query: str, documents: list[str]) -> list[float]:
        """按查询字符与文档字符的交集大小返回固定规则分数。

        仅模拟检索接口，不运行真实嵌入模型。
        """
        query_terms = set(query)
        return [float(len(query_terms.intersection(document))) for document in documents]

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        """复用字符交集评分作为重排替身。

        让报告结构测试保持确定性。
        """
        return self.score(query, documents)


def test_evaluate_bge_strategy_reports_models_latency_and_safety_misses():
    """注入实验检索替身运行评估。

    检查模型名称、延迟、安全遗漏率和索引大小字段，不据此宣称真实模型效果。
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        settings = EvalSettings().model_copy(update={
            "rag_eval_dataset": str(DATASET),
            "rag_eval_database_url": f"sqlite:///{tmp_path / 'eval.db'}",
            "knowledge_vector_enabled": False,
            "rag_eval_bge_output": str(tmp_path / "bge-report.json"),
            "rag_eval_bge_summary_output": str(tmp_path / "bge-summary.json"),
        })
        retriever = BgeM3Retriever(_FakeBgeEvalBackend(), candidate_pool=12, rerank=True)

        report = evaluate(settings, strategy="bge-m3-rerank", retriever=retriever)

    assert report["retrieval"] == "BGE-M3 + bge-reranker-v2-m3"
    assert report["embeddingModel"] == "fake-bge-m3"
    assert report["rerankerModel"] == "fake-bge-reranker"
    assert report["p95LatencyMs"] >= 0.0
    assert 0.0 <= report["safetyCriticalMissRate"] <= 1.0
    assert report["indexSizeBytes"] > 0


def test_evaluate_llm_rerank_writes_summary_file():
    """运行模型重排方案并读取摘要。

    检查重排专用输出文件存在且名称正确。
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        summary_path = tmp_path / "llm-rerank.json"
        settings = EvalSettings().model_copy(update={
            "rag_eval_dataset": str(DATASET),
            "rag_eval_database_url": f"sqlite:///{tmp_path / 'eval.db'}",
            "knowledge_vector_enabled": False,
            "ai_provider": "mock",
            "rag_eval_ai_provider": "mock",
            "rag_eval_output": str(tmp_path / "report.json"),
            "rag_eval_baseline_output": str(tmp_path / "baseline.json"),
            "rag_eval_llm_rerank_output": str(summary_path),
        })
        evaluate(settings, strategy="llm-rerank")
        assert summary_path.exists()
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        assert "LLM-rerank" in summary["retrieval"]


# ---------------------------------------------------------------------------
# Comparison summary (issue 01: evaluation closed loop)
# ---------------------------------------------------------------------------

def _write_fake_summary(path: Path, metrics: dict) -> None:
    """按指定指标生成一份简化摘要文件。

    用于比较逻辑测试，不运行知识检索。
    """
    summary = {
        "createdAt": "2026-07-26T00:00:00",
        "dataset": "test.json",
        "topK": 4,
        "retrieval": "test",
        "totalCases": 60,
        "averageFirstRelevantRank": 1.2,
        **metrics,
    }
    path.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")


def test_build_comparison_all_strategies_present():
    """准备六种方案的摘要和可手算指标。

    检查全部可用、最佳方案及相对基线差值正确，并优先以融合方案为当前参照。
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _write_fake_summary(tmp_path / "baseline.json", {"recallAtK": 0.80, "precisionAtK": 0.50, "mrr": 0.70, "ndcgAtK": 0.60, "hitRate": 0.90})
        _write_fake_summary(tmp_path / "multi-query.json", {"recallAtK": 0.85, "precisionAtK": 0.55, "mrr": 0.75, "ndcgAtK": 0.65, "hitRate": 0.92})
        _write_fake_summary(tmp_path / "hybrid-rrf.json", {"recallAtK": 0.82, "precisionAtK": 0.52, "mrr": 0.72, "ndcgAtK": 0.62, "hitRate": 0.88})
        _write_fake_summary(tmp_path / "llm-rerank.json", {"recallAtK": 0.90, "precisionAtK": 0.60, "mrr": 0.80, "ndcgAtK": 0.70, "hitRate": 0.95})
        _write_fake_summary(tmp_path / "bge.json", {"recallAtK": 0.88, "precisionAtK": 0.58, "mrr": 0.78, "ndcgAtK": 0.68, "hitRate": 0.94})
        _write_fake_summary(tmp_path / "bge-rerank.json", {"recallAtK": 0.92, "precisionAtK": 0.62, "mrr": 0.82, "ndcgAtK": 0.72, "hitRate": 0.96})

        settings = EvalSettings().model_copy(update={
            "rag_eval_baseline_output": str(tmp_path / "baseline.json"),
            "rag_eval_multi_query_output": str(tmp_path / "multi-query.json"),
            "rag_eval_hybrid_rrf_output": str(tmp_path / "hybrid-rrf.json"),
            "rag_eval_llm_rerank_output": str(tmp_path / "llm-rerank.json"),
            "rag_eval_bge_summary_output": str(tmp_path / "bge.json"),
            "rag_eval_bge_rerank_summary_output": str(tmp_path / "bge-rerank.json"),
        })

        comparison = build_comparison(settings)

        assert len(comparison["strategies"]) == 6
        assert all(s["available"] for s in comparison["strategies"])
        assert comparison["strategies"][0]["name"] == "baseline"
        assert comparison["strategies"][0]["metrics"]["mrr"] == 0.70
        assert comparison["delta"]["bestStrategy"] == "bge-m3-rerank"
        assert abs(comparison["delta"]["metrics"]["mrr"] - 0.12) < 1e-6
        assert comparison["currentReferenceStrategy"] == "hybrid-rrf"


def test_build_comparison_missing_strategy():
    """只创建基线摘要，其余路径不存在。

    检查缺失方案明确标为不可用，且没有足够方案时不产生差值。
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _write_fake_summary(tmp_path / "baseline.json", {"recallAtK": 0.80, "precisionAtK": 0.50, "mrr": 0.70, "ndcgAtK": 0.60, "hitRate": 0.90})

        settings = EvalSettings().model_copy(update={
            "rag_eval_baseline_output": str(tmp_path / "baseline.json"),
            "rag_eval_multi_query_output": str(tmp_path / "missing-mq.json"),
            "rag_eval_hybrid_rrf_output": str(tmp_path / "missing-rrf.json"),
            "rag_eval_llm_rerank_output": str(tmp_path / "missing-rerank.json"),
            "rag_eval_bge_summary_output": str(tmp_path / "missing-bge.json"),
            "rag_eval_bge_rerank_summary_output": str(tmp_path / "missing-bge-rerank.json"),
        })

        comparison = build_comparison(settings)

        assert comparison["strategies"][0]["available"] is True
        assert comparison["strategies"][1]["available"] is False
        assert comparison["strategies"][2]["available"] is False
        assert comparison["strategies"][3]["available"] is False
        assert comparison["delta"] is None


def test_build_comparison_only_baseline_no_delta():
    """实际准备基线和多查询两份摘要。

    虽然旧函数名写无差值，当前断言检查多查询为最佳且差值为 0.05。
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _write_fake_summary(tmp_path / "baseline.json", {"recallAtK": 0.80, "precisionAtK": 0.50, "mrr": 0.70, "ndcgAtK": 0.60, "hitRate": 0.90})
        _write_fake_summary(tmp_path / "multi-query.json", {"recallAtK": 0.85, "precisionAtK": 0.55, "mrr": 0.75, "ndcgAtK": 0.65, "hitRate": 0.92})

        settings = EvalSettings().model_copy(update={
            "rag_eval_baseline_output": str(tmp_path / "baseline.json"),
            "rag_eval_multi_query_output": str(tmp_path / "multi-query.json"),
            "rag_eval_hybrid_rrf_output": str(tmp_path / "missing.json"),
            "rag_eval_llm_rerank_output": str(tmp_path / "missing2.json"),
            "rag_eval_bge_summary_output": str(tmp_path / "missing-bge.json"),
            "rag_eval_bge_rerank_summary_output": str(tmp_path / "missing-bge-rerank.json"),
        })

        comparison = build_comparison(settings)

        assert comparison["delta"]["bestStrategy"] == "multi-query"
        assert abs(comparison["delta"]["metrics"]["mrr"] - 0.05) < 1e-6


def test_format_comparison_markdown_has_table():
    """构造包含可用、缺失和差值的比较数据。

    检查表格列、方案名称、未运行标记和差值行都出现。
    """
    comparison = {
        "strategies": [
            {"name": "baseline", "available": True, "metrics": {"recallAtK": 0.80, "precisionAtK": 0.50, "mrr": 0.70, "ndcgAtK": 0.60, "hitRate": 0.90}},
            {"name": "multi-query", "available": True, "metrics": {"recallAtK": 0.85, "precisionAtK": 0.55, "mrr": 0.75, "ndcgAtK": 0.65, "hitRate": 0.92}},
            {"name": "hybrid-rrf", "available": False, "metrics": None},
            {"name": "llm-rerank", "available": True, "metrics": {"recallAtK": 0.90, "precisionAtK": 0.60, "mrr": 0.80, "ndcgAtK": 0.70, "hitRate": 0.95}},
        ],
        "delta": {
            "bestStrategy": "llm-rerank",
            "metrics": {"recallAtK": 0.10, "precisionAtK": 0.10, "mrr": 0.10, "ndcgAtK": 0.10, "hitRate": 0.05},
        },
    }

    md = format_comparison_markdown(comparison)

    assert "Recall" in md
    assert "Precision" in md
    assert "MRR" in md
    assert "baseline" in md
    assert "multi-query" in md
    assert "未运行" in md
    assert "llm-rerank" in md
    assert "Δ" in md


def test_retrieval_decision_rejects_quality_gain_that_breaks_latency_budget():
    """候选质量更好且安全遗漏不变，但延迟远超预算。

    检查质量和安全通过仍不能允许采用候选。
    """
    baseline = {
        "recallAtK": 0.95,
        "mrr": 0.86,
        "safetyCriticalMissRate": 0.03,
        "p95LatencyMs": 4.0,
    }
    candidate = {
        "recallAtK": 0.99,
        "mrr": 0.92,
        "safetyCriticalMissRate": 0.03,
        "p95LatencyMs": 120.0,
    }

    decision = build_retrieval_decision(baseline, candidate)

    assert decision["qualityGate"] is True
    assert decision["safetyGate"] is True
    assert decision["latencyGate"] is False
    assert decision["deployable"] is False
    assert decision["decision"] == "keep-current-retriever"


def test_format_comparison_markdown_no_delta():
    """只给基线数据并明确无差值。

    检查表格保留缺失方案提示但不生成差值行。
    """
    comparison = {
        "strategies": [
            {"name": "baseline", "available": True, "metrics": {"recallAtK": 0.80, "precisionAtK": 0.50, "mrr": 0.70, "ndcgAtK": 0.60, "hitRate": 0.90}},
            {"name": "multi-query", "available": False, "metrics": None},
            {"name": "hybrid-rrf", "available": False, "metrics": None},
            {"name": "llm-rerank", "available": False, "metrics": None},
        ],
        "delta": None,
    }

    md = format_comparison_markdown(comparison)

    assert "baseline" in md
    assert "未运行" in md
    assert "Δ" not in md
