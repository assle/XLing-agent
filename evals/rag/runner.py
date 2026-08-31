import json
import logging
import math
import time
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, make_url
from sqlalchemy.orm import sessionmaker

from app.core.bootstrap import create_schema, seed_data
from app.core.time import utc_now
from app.core.versioning import ArtifactVersionResolver
from app.services.bge_retrieval import BgeM3Retriever
from app.services.knowledge import KnowledgeService
from app.services.vector_store import FALLBACK_RETRIEVAL_LABEL, PRIMARY_RETRIEVAL_LABEL
from evals.config import EvalSettings, get_eval_settings

logger = logging.getLogger(__name__)


def evaluate(
    settings: EvalSettings | None = None,
    strategy: str = "baseline",
    retriever: BgeM3Retriever | None = None,
) -> dict:
    """Run the RAG eval against a self-contained SQLite knowledge base.

    Builds its own SQLite engine (no MySQL/Docker) and an isolated Chroma store
    so the run is repeatable and does not mutate dev state. Writes the full
    per-case report to ``rag_eval_output`` and a compact 5-metric baseline
    summary to ``rag_eval_baseline_output``.

    Supported strategies: baseline, multi-query, hybrid-rrf, llm-rerank.
    Each writes its own summary file for cross-run comparison.
    """
    settings = settings or get_eval_settings()
    eval_settings = _eval_settings(settings)
    if strategy in {"bge-m3", "bge-m3-rerank"}:
        retriever = retriever or BgeM3Retriever.from_settings(eval_settings)
    engine = _build_engine(eval_settings.rag_eval_database_url)
    create_schema(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    try:
        seed_data(db, eval_settings)
        ai_client = _build_ai_client(eval_settings, strategy)
        service = KnowledgeService(
            db,
            eval_settings,
            ai_client=ai_client,
            retriever=retriever,
        )
        retrieval_label = _retrieval_label(service, strategy)
        strategy_label = _strategy_label(strategy, retrieval_label)
        if strategy != "baseline":
            logger.info("RAG eval strategy: %s (%s)", strategy, strategy_label)
        if strategy not in {"bge-m3", "bge-m3-rerank"} and retrieval_label != PRIMARY_RETRIEVAL_LABEL:
            logger.warning(
                "RAG eval 向量路径不可用（%s），基线走 %s 兜底；"
                "设置 OPENAI_API_KEY 并安装 chromadb 可获得真向量基线。",
                getattr(service.vector_store, "error", "") or "未知原因",
                FALLBACK_RETRIEVAL_LABEL,
            )
        dataset_path = Path(settings.rag_eval_dataset)
        raw = dataset_path.read_text(encoding="utf-8")
        try:
            cases = json.loads(raw)
        except json.JSONDecodeError:
            cases = [json.loads(line) for line in raw.splitlines() if line.strip()]
        retrieve_fn = _build_retrieve_fn(service, strategy, settings)
        results = [evaluate_case(retrieve_fn, case, settings.rag_eval_top_k) for case in cases]
        retrieval_label = _retrieval_label(service, strategy)
        strategy_label = _strategy_label(strategy, retrieval_label)
        report = compute_report(
            results,
            settings.rag_eval_dataset,
            settings.rag_eval_top_k,
            strategy_label,
        )
        report["embeddingModel"] = (
            retriever.embedding_model if retriever else eval_settings.openai_embedding_model
        )
        report["rerankerModel"] = retriever.reranker_model if retriever else ""
        report["indexSizeBytes"] = retriever.index_size_bytes if retriever else 0
        report["corpusSizeBytes"] = retriever.corpus_size_bytes if retriever else 0
        report["comparisonEligible"] = (
            strategy in {"bge-m3", "bge-m3-rerank"}
            or (
                strategy == "hybrid-rrf"
                and service.vector_store.can_embed
                and not service.vector_fallback_used
            )
        )
        report["artifactVersion"] = ArtifactVersionResolver(eval_settings).current(
            settings.rag_eval_dataset
        ).to_dict()
        output_path, summary_path = _strategy_paths(strategy, settings)
        _write_json(report, output_path)
        _write_json(build_eval_summary(report), summary_path)
        return report
    finally:
        db.close()
        engine.dispose()


def compute_report(
    results: list[dict], dataset: str, top_k: int, retrieval_label: str
) -> dict:
    total = max(1, len(results))
    hits = [item for item in results if item["hit"]]
    safety_cases = [item for item in results if item.get("safetyCritical", False)]
    safety_misses = [item for item in safety_cases if not item["hit"]]
    latencies = sorted(item.get("latencyMs", 0.0) for item in results)
    p95_index = max(0, math.ceil(len(latencies) * 0.95) - 1)
    return {
        "createdAt": utc_now().isoformat(),
        "dataset": dataset,
        "topK": top_k,
        "retrieval": retrieval_label,
        "totalCases": len(results),
        "recallAtK": sum(item["recallAtK"] for item in results) / total,
        "precisionAtK": sum(item["precisionAtK"] for item in results) / total,
        "mrr": sum(item["reciprocalRank"] for item in results) / total,
        "ndcgAtK": sum(item["ndcgAtK"] for item in results) / total,
        "hitRate": len(hits) / total,
        "averageFirstRelevantRank": sum(item["firstRelevantRank"] for item in hits) / max(1, len(hits)),
        "p95LatencyMs": latencies[p95_index] if latencies else 0.0,
        "safetyCriticalCases": len(safety_cases),
        "safetyCriticalMissRate": len(safety_misses) / max(1, len(safety_cases)),
        "results": results,
    }


def build_eval_summary(report: dict) -> dict:
    """Compact 5-metric summary for cross-run comparison (baseline vs enhancements).

    Kept separate from the full report so issues 02-04 can write their own
    summary files (multi-query / hybrid-rrf / llm-rerank) against this baseline.
    """
    return {
        "createdAt": report["createdAt"],
        "dataset": report["dataset"],
        "topK": report["topK"],
        "retrieval": report["retrieval"],
        "totalCases": report["totalCases"],
        "recallAtK": report["recallAtK"],
        "precisionAtK": report["precisionAtK"],
        "mrr": report["mrr"],
        "ndcgAtK": report["ndcgAtK"],
        "hitRate": report["hitRate"],
        "averageFirstRelevantRank": report["averageFirstRelevantRank"],
        "p95LatencyMs": report.get("p95LatencyMs", 0.0),
        "safetyCriticalCases": report.get("safetyCriticalCases", 0),
        "safetyCriticalMissRate": report.get("safetyCriticalMissRate", 0.0),
        "embeddingModel": report.get("embeddingModel", ""),
        "rerankerModel": report.get("rerankerModel", ""),
        "indexSizeBytes": report.get("indexSizeBytes", 0),
        "corpusSizeBytes": report.get("corpusSizeBytes", 0),
        "comparisonEligible": report.get("comparisonEligible", False),
        "artifactVersion": report.get("artifactVersion", {}),
    }


def evaluate_case(retrieve_fn, case: dict, top_k: int) -> dict:
    started = time.perf_counter()
    retrieved = retrieve_fn(case["question"], top_k)
    latency_ms = (time.perf_counter() - started) * 1000.0
    expected_sources = {source.lower() for source in case.get("expectedSources", [])}
    expected_terms = [term.lower() for term in case.get("expectedTerms", [])]
    items = []
    first_rank = 0
    relevant_count = 0
    for index, item in enumerate(retrieved, start=1):
        relevant = is_relevant(item.source, item.content, expected_sources, expected_terms)
        if relevant:
            relevant_count += 1
            if first_rank == 0:
                first_rank = index
        items.append({
            "rank": index,
            "chunkId": item.chunk_id,
            "source": item.source,
            "score": item.score,
            "relevant": relevant,
            "preview": " ".join(item.content.split())[:160],
        })
    hit = first_rank > 0
    return {
        "id": case["id"],
        "question": case["question"],
        "expectedSources": case.get("expectedSources", []),
        "expectedTerms": case.get("expectedTerms", []),
        "retrieved": items,
        "hit": hit,
        "firstRelevantRank": first_rank,
        "recallAtK": 1.0 if hit else 0.0,
        "precisionAtK": relevant_count / top_k if top_k > 0 else 0.0,
        "reciprocalRank": 1.0 / first_rank if hit else 0.0,
        "ndcgAtK": ndcg(items),
        "latencyMs": round(latency_ms, 4),
        "safetyCritical": "risk-policy.md" in expected_sources,
    }


def is_relevant(source: str, content: str, expected_sources: set[str], expected_terms: list[str]) -> bool:
    if source.lower() in expected_sources:
        return True
    lower = content.lower()
    return any(len(term) >= 2 and term in lower for term in expected_terms)


def ndcg(items: list[dict]) -> float:
    dcg = 0.0
    relevant = 0
    for index, item in enumerate(items):
        if item["relevant"]:
            relevant += 1
            dcg += 1.0 / math.log(index + 2.0)
    if relevant == 0:
        return 0.0
    ideal = sum(1.0 / math.log(index + 2.0) for index in range(relevant))
    return dcg / ideal


def _eval_settings(settings: EvalSettings) -> EvalSettings:
    """Copy of settings pointing Chroma at the isolated eval store."""
    return settings.model_copy(update={
        "chroma_persist_dir": settings.rag_eval_chroma_persist_dir,
        "chroma_collection_name": settings.rag_eval_chroma_collection_name,
        "ai_provider": settings.rag_eval_ai_provider,
        "openai_api_key": settings.rag_eval_api_key or settings.openai_api_key,
        "openai_base_url": settings.rag_eval_base_url or settings.openai_base_url,
        "openai_model": settings.rag_eval_model or settings.openai_model,
        "ai_max_tokens": 4096,
    })


def _build_engine(url: str):
    kwargs: dict[str, Any] = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        _ensure_sqlite_parent_dir(url)
    return create_engine(url, **kwargs)


def _ensure_sqlite_parent_dir(url: str) -> None:
    database = make_url(url).database
    if not database or database == ":memory:":
        return
    Path(database).parent.mkdir(parents=True, exist_ok=True)


def _retrieval_label(service: KnowledgeService, strategy: str = "baseline") -> str:
    if strategy == "bge-m3":
        return "BGE-M3"
    if strategy == "bge-m3-rerank":
        return "BGE-M3 + bge-reranker-v2-m3"
    if service.vector_store.can_embed and not getattr(
        service,
        "vector_fallback_used",
        False,
    ):
        return PRIMARY_RETRIEVAL_LABEL
    return FALLBACK_RETRIEVAL_LABEL


def _write_json(data: dict, path: str) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_ai_client(settings: EvalSettings, strategy: str):
    """Create an AiClient for strategies that need LLM (multi-query, llm-rerank)."""
    if strategy in ("multi-query", "hybrid-rrf", "llm-rerank"):
        from app.services.ai import AiClient
        return AiClient(settings)
    return None


def _strategy_label(strategy: str, retrieval_label: str) -> str:
    labels = {
        "baseline": retrieval_label,
        "multi-query": f"multi-query + {retrieval_label}",
        "hybrid-rrf": f"multi-query + hybrid-RRF (k=60) + {retrieval_label}",
        "llm-rerank": f"multi-query + hybrid-RRF + LLM-rerank + {retrieval_label}",
        "bge-m3": "BGE-M3",
        "bge-m3-rerank": "BGE-M3 + bge-reranker-v2-m3",
    }
    return labels.get(strategy, retrieval_label)


def _build_retrieve_fn(service: KnowledgeService, strategy: str, settings: EvalSettings):
    """Return a (query, top_k) -> list[SearchResult] callable for the given strategy."""
    if strategy == "baseline":
        return lambda q, k: service.retrieve(q, k)
    if strategy == "multi-query":
        return lambda q, k: service.retrieve_multi_query(q, k)
    if strategy == "hybrid-rrf":
        return lambda q, k: service.retrieve_multi_query(q, k, base_retrieve=service.retrieve_hybrid_rrf)
    if strategy == "llm-rerank":
        from app.services.knowledge import SearchResult as _SR
        pool = settings.rag_eval_rerank_candidate_pool

        def _retrieve(q: str, k: int):
            candidates = service.retrieve_multi_query(q, pool, base_retrieve=service.retrieve_hybrid_rrf)
            if not candidates or not service.ai_client:
                return candidates[:k] if candidates else []
            candidate_dicts = [
                {"source": c.source, "content": c.content, "chunk_id": c.chunk_id}
                for c in candidates
            ]
            scored = service.ai_client.rerank(q, candidate_dicts)
            index_to_score = {idx: score for idx, score in scored}
            reranked = [
                _SR(c.chunk_id, c.source, c.content, index_to_score.get(i, 0.0))
                for i, c in enumerate(candidates)
            ]
            reranked.sort(key=lambda r: r.score, reverse=True)
            return reranked[:k]
        return _retrieve
    return lambda q, k: service.retrieve(q, k)


def _strategy_paths(strategy: str, settings: EvalSettings) -> tuple[str, str]:
    """Return (report_path, summary_path) for the given strategy."""
    path_map = {
        "baseline": (settings.rag_eval_output, settings.rag_eval_baseline_output),
        "multi-query": (
            settings.rag_eval_output.replace(".json", "-multi-query.json"),
            settings.rag_eval_multi_query_output,
        ),
        "hybrid-rrf": (
            settings.rag_eval_output.replace(".json", "-hybrid-rrf.json"),
            settings.rag_eval_hybrid_rrf_output,
        ),
        "llm-rerank": (
            settings.rag_eval_output.replace(".json", "-llm-rerank.json"),
            settings.rag_eval_llm_rerank_output,
        ),
        "bge-m3": (
            settings.rag_eval_bge_output,
            settings.rag_eval_bge_summary_output,
        ),
        "bge-m3-rerank": (
            settings.rag_eval_bge_rerank_output,
            settings.rag_eval_bge_rerank_summary_output,
        ),
    }
    return path_map.get(strategy, path_map["baseline"])


COMPARISON_METRICS = ["recallAtK", "precisionAtK", "mrr", "ndcgAtK", "hitRate"]
DECISION_METRICS = ["safetyCriticalMissRate", "p95LatencyMs"]
COMPARISON_STRATEGIES = [
    "baseline",
    "multi-query",
    "hybrid-rrf",
    "llm-rerank",
    "bge-m3",
    "bge-m3-rerank",
]


def build_comparison(settings: EvalSettings | None = None) -> dict:
    """Read 4 strategy summaries and return comparison data with delta.

    Missing summary files are marked as unavailable rather than raising.
    Delta is computed as best strategy (highest MRR) minus baseline.
    """
    settings = settings or get_eval_settings()
    path_map = {
        "baseline": settings.rag_eval_baseline_output,
        "multi-query": settings.rag_eval_multi_query_output,
        "hybrid-rrf": settings.rag_eval_hybrid_rrf_output,
        "llm-rerank": settings.rag_eval_llm_rerank_output,
        "bge-m3": settings.rag_eval_bge_summary_output,
        "bge-m3-rerank": settings.rag_eval_bge_rerank_summary_output,
    }

    strategies: list[dict[str, Any]] = []
    for name in COMPARISON_STRATEGIES:
        path = Path(path_map[name])
        if path.exists():
            summary = json.loads(path.read_text(encoding="utf-8"))
            metrics = {k: summary.get(k) for k in [*COMPARISON_METRICS, *DECISION_METRICS]}
            strategies.append({
                "name": name,
                "available": True,
                "comparisonEligible": summary.get("comparisonEligible", True),
                "metrics": metrics,
            })
        else:
            strategies.append({
                "name": name,
                "available": False,
                "comparisonEligible": False,
                "metrics": None,
            })

    available = [s for s in strategies if s["available"]]
    baseline = next(
        (s for s in strategies if s["name"] == "baseline" and s["available"]), None
    )
    current_reference = next(
        (s for s in strategies if s["name"] == "hybrid-rrf" and s["available"]),
        baseline,
    )

    delta = None
    if baseline and len(available) > 1:
        best = max(available, key=lambda s: (s["metrics"] or {}).get("mrr", 0) or 0)
        baseline_metrics = baseline["metrics"] or {}
        best_metrics = best["metrics"] or {}
        delta_metrics = {}
        for metric in COMPARISON_METRICS:
            base_val = baseline_metrics.get(metric, 0) or 0
            best_val = best_metrics.get(metric, 0) or 0
            delta_metrics[metric] = round(best_val - base_val, 6)
        delta = {"bestStrategy": best["name"], "metrics": delta_metrics}

    deployment_decisions = {}
    if current_reference:
        for candidate_name in ("bge-m3", "bge-m3-rerank"):
            candidate = next(
                (item for item in strategies if item["name"] == candidate_name and item["available"]),
                None,
            )
            if candidate:
                deployment_decisions[candidate_name] = build_retrieval_decision(
                    current_reference["metrics"] or {},
                    candidate["metrics"] or {},
                    fair_comparison=bool(current_reference["comparisonEligible"]),
                )
    return {
        "strategies": strategies,
        "delta": delta,
        "currentReferenceStrategy": (
            current_reference["name"] if current_reference else None
        ),
        "currentReferenceEligible": (
            bool(current_reference["comparisonEligible"]) if current_reference else False
        ),
        "deploymentDecisions": deployment_decisions,
    }


def build_retrieval_decision(
    baseline: dict,
    candidate: dict,
    *,
    fair_comparison: bool = True,
) -> dict:
    recall_gain = (candidate.get("recallAtK") or 0.0) - (baseline.get("recallAtK") or 0.0)
    mrr_gain = (candidate.get("mrr") or 0.0) - (baseline.get("mrr") or 0.0)
    quality_gate = recall_gain >= 0.03 or mrr_gain >= 0.03
    safety_gate = (candidate.get("safetyCriticalMissRate") or 0.0) <= (
        baseline.get("safetyCriticalMissRate") or 0.0
    )
    baseline_latency = baseline.get("p95LatencyMs") or 0.0
    candidate_latency = candidate.get("p95LatencyMs") or 0.0
    latency_gate = baseline_latency > 0 and candidate_latency <= baseline_latency * 1.5
    deployable = fair_comparison and quality_gate and safety_gate and latency_gate
    return {
        "recallGain": recall_gain,
        "mrrGain": mrr_gain,
        "qualityGate": quality_gate,
        "safetyGate": safety_gate,
        "latencyGate": latency_gate,
        "fairComparisonGate": fair_comparison,
        "deployable": deployable,
        "decision": (
            "enable-candidate-retriever"
            if deployable
            else "keep-current-retriever"
            if fair_comparison
            else "rerun-current-reference-with-required-vector-dependencies"
        ),
    }


def format_comparison_markdown(comparison: dict) -> str:
    """Render comparison dict as a Markdown table."""
    headers = ["策略", "Recall@K", "Precision@K", "MRR", "NDCG@K", "HitRate"]
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(["---"] * len(headers)) + "|",
    ]

    for s in comparison["strategies"]:
        if s["available"]:
            row = [s["name"]]
            for key in COMPARISON_METRICS:
                val = s["metrics"].get(key)
                row.append(f"{val:.4f}" if val is not None else "-")
        else:
            row = [s["name"]] + ["未运行"] * len(COMPARISON_METRICS)
        lines.append("| " + " | ".join(row) + " |")

    delta = comparison.get("delta")
    if delta:
        row = [f"Δ ({delta['bestStrategy']} vs base)"]
        for key in COMPARISON_METRICS:
            val = delta["metrics"].get(key, 0)
            sign = "+" if val >= 0 else ""
            row.append(f"{sign}{val:.4f}")
        lines.append("| " + " | ".join(row) + " |")

    return "\n".join(lines)


def write_comparison(settings: EvalSettings | None = None) -> tuple[str, str]:
    """Build comparison and write JSON + Markdown outputs. Returns (json_path, md_path)."""
    settings = settings or get_eval_settings()
    comparison = build_comparison(settings)
    md = format_comparison_markdown(comparison)

    json_path = Path(settings.rag_eval_comparison_output)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8")

    md_path = Path(settings.rag_eval_comparison_md_output)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(md, encoding="utf-8")

    return str(json_path), str(md_path)


if __name__ == "__main__":
    import sys
    strategy = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    valid = {
        "baseline",
        "multi-query",
        "hybrid-rrf",
        "llm-rerank",
        "bge-m3",
        "bge-m3-rerank",
        "comparison",
    }
    if strategy not in valid:
        print(f"Unknown strategy: {strategy}. Valid: {valid}")
        sys.exit(1)
    if strategy == "comparison":
        json_path, md_path = write_comparison()
        comparison = build_comparison()
        print(format_comparison_markdown(comparison))
        print(f"\njson={json_path}")
        print(f"markdown={md_path}")
        sys.exit(0)
    report = evaluate(strategy=strategy)
    print(f"RAG evaluation completed (strategy={strategy}).")
    for key in [
        "totalCases", "topK", "retrieval",
        "recallAtK", "precisionAtK", "mrr", "ndcgAtK", "hitRate",
        "averageFirstRelevantRank",
    ]:
        print(f"{key}={report[key]}")
    _, summary_path = _strategy_paths(strategy, get_eval_settings())
    print(f"summary={summary_path}")
