"""Verify adopted retrieval with real HTTP, isolated SQL/Chroma and load timings."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np
from rank_bm25 import BM25Okapi
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.bootstrap import create_schema
from app.core.config import Settings
from app.models.entities import KnowledgeChunk
from app.services.knowledge import KnowledgeService, tokenize
from app.services.lexical_index import LexicalIndex, lexical_index
from evals.rag.closing import ROOT, fingerprint, load_cases
from evals.rag.optimization import Embeddings, fixed_chunks, measure, summary


def benchmark_hybrid(factory, settings, cases, cache_directory: Path) -> dict:
    """Isolate index work with real cached vectors and identical fusion/SQL/Chroma."""
    class RebuiltIndex:
        def __init__(self, documents):
            self.model = BM25Okapi([tokenize(document) for document in documents])

        def scores(self, query):
            return dict(enumerate(self.model.get_scores(tokenize(query))))

    results = {"rebuildBM25": [], "cachedPostings": []}
    with factory() as db:
        service = KnowledgeService(db, settings)
        embeddings = Embeddings(service.vector_store, cache_directory)
        query_vectors = embeddings.get([case["question"] for case in cases])
    for i, case in enumerate(cases):
        rankings = {}
        for name in (("rebuildBM25", "cachedPostings") if i % 2 == 0 else ("cachedPostings", "rebuildBM25")):
            with factory() as db:
                service = KnowledgeService(db, settings)
                service.vector_store.embed_texts = lambda texts: [query_vectors[i].tolist()]
                with patch("app.services.knowledge.lexical_index", RebuiltIndex if name == "rebuildBM25" else lexical_index):
                    # Warm up both paths; compare unchanged math and ordering.
                    service.retrieve(case["question"], 4)
                    samples = []
                    for _ in range(3):
                        found, ms = timed(lambda: service.retrieve(case["question"], 4))
                        samples.append(ms)
                    results[name].append(float(np.median(samples)))
                    rankings[name] = [(r.source, r.content) for r in found]
        if rankings["rebuildBM25"] != rankings["cachedPostings"]:
            raise AssertionError("Index optimization changed hybrid ranking")
    return {"scope": "same SQL/Chroma/corpus/fusion; cached REAL query vectors; excludes remote latency",
            "allRankingsEqual": True,
            **{name: {"p50Ms": float(np.median(times)), "p95Ms": float(np.quantile(times, .95)), "timesMs": times}
               for name, times in results.items()}}


def timed(action):
    tick = time.perf_counter()
    value = action()
    return value, (time.perf_counter() - tick) * 1000


def run(comparison: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    report = {"status": "running", "comparisonSHA256": fingerprint(comparison), "arms": {}, "indexing": {},
              "scope": "isolated SQL/Chroma, real HTTP embeddings, fresh service per query; no answer generation"}
    experiment = json.loads(comparison.read_text())
    cases = load_cases(ROOT / "evals/rag/closing-40.jsonl")
    files = sorted((ROOT / "app/knowledge").glob("*.md")) + [ROOT / "evals/rag/closing-deployment-resources.md"]
    if experiment["datasetSHA256"] != fingerprint(ROOT / "evals/rag/closing-40.jsonl"):
        raise ValueError("Dataset changed after selection")
    if experiment["knowledgeSourceSHA256"] != {p.name: fingerprint(p) for p in files}:
        raise ValueError("Knowledge changed after selection")
    if experiment["selected"] != "semantic512-convex98":
        raise ValueError("Adopted method does not match selected candidate")
    engines = {}
    factories = {}
    settings_by_arm = {}
    path = output / "verification.json"
    try:
        for arm in ("baseline", "adopted"):
            settings = Settings(chroma_persist_dir=str(output / arm / "chroma"),
                                chroma_collection_name="verified_retrieval", knowledge_vector_enabled=True,
                                knowledge_vector_required=True)
            if settings.openai_embedding_model != experiment["embeddingModel"]:
                raise ValueError("Embedding model differs from selection")
            settings_by_arm[arm] = settings
            engine = create_engine(f"sqlite:///{output / (arm + '.db')}")
            engines[arm] = engine
            create_schema(engine)
            factories[arm] = sessionmaker(bind=engine)
            with factories[arm]() as db:
                service = KnowledgeService(db, settings)
                original_embed = service.vector_store.embed_texts
                calls = []
                def tracked(texts):
                    vectors, ms = timed(lambda: original_embed(texts))
                    calls.append({"texts": len(texts), "characters": sum(map(len, texts)), "latencyMs": ms})
                    return vectors
                service.vector_store.embed_texts = tracked
                start = time.perf_counter()
                if arm == "baseline":
                    for file in files:
                        db.add_all([KnowledgeChunk(source=file.name, source_index=i, content=chunk)
                                    for i, chunk in enumerate(fixed_chunks(file.read_text()))])
                    db.commit()
                    service.rebuild_vector_index()
                else:
                    for file in files:
                        service.ensure_source(file.name, file.read_text())
                indexing_ms = (time.perf_counter() - start) * 1000
                report["indexing"][arm] = {"coldMs": indexing_ms, "embeddingCalls": list(calls),
                                           "chunks": service.count()}
                if arm == "adopted":
                    expected = experiment["corpora"]["semantic512"]["chunks"]
                    actual = db.query(KnowledgeChunk).order_by(KnowledgeChunk.source, KnowledgeChunk.source_index).all()
                    report["indexing"][arm]["samePassagesAsExperiment"] = (
                        [(r.source, r.content) for r in actual] == sorted(
                            [(r["source"], r["content"]) for r in expected], key=lambda r: r[0]))
                    calls.clear()
                    _, warm_ms = timed(lambda: [service.ensure_source(p.name, p.read_text()) for p in files])
                    report["indexing"][arm].update(warmEnsureMs=warm_ms, warmEmbeddingCalls=len(calls))
                    if calls:
                        raise AssertionError("Unchanged sources must not re-embed sentences")
                print(json.dumps({"indexed": arm, "ms": indexing_ms}), flush=True)
        for name in ["baseline", "adopted", "baseline-local", "adopted-local"]:
            report["arms"][name] = {"cases": []}
        expected_rows = {r["id"]: r for r in experiment["arms"][experiment["selected"]]["cases"]}
        for i, case in enumerate(cases):
            for arm in (("baseline", "adopted") if i % 2 == 0 else ("adopted", "baseline")):
                with factories[arm]() as db:
                    settings = settings_by_arm[arm]
                    service = KnowledgeService(db, settings)
                    original_embed = service.vector_store.embed_texts
                    embedding_ms = []
                    def query_embed(texts):
                        vectors, ms = timed(lambda: original_embed(texts))
                        embedding_ms.append(ms)
                        return vectors
                    service.vector_store.embed_texts = query_embed
                    def retrieve():
                        if arm == "baseline":
                            return service._expand_best(service._retrieve_vector(case["question"], 4), 4)
                        return service.retrieve(case["question"], 4)
                    found, ms = timed(retrieve)
                    row = measure(case, found, ms)
                    row["embeddingMs"] = sum(embedding_ms)
                    row["localAndSQLMs"] = ms - sum(embedding_ms)
                    if arm == "adopted":
                        expected = expected_rows[case["id"]]
                        row["matchesExperiment"] = ([r["contentSHA256"] for r in row["retrieved"]] ==
                                                     [r["contentSHA256"] for r in expected["retrieved"]])
                    report["arms"][arm]["cases"].append(row)
                    found, ms = timed(lambda: service._retrieve_hybrid(case["question"], 4))
                    report["arms"][arm + "-local"]["cases"].append(measure(case, found, ms))
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps({"questions": i + 1}), flush=True)
        for arm in report["arms"].values():
            arm["summary"] = summary(arm["cases"])

        report["hybridIndexAblation"] = benchmark_hybrid(
            factories["adopted"], settings_by_arm["adopted"], cases, comparison.parent.parent / "embedding-cache")

        # Repeated real passages are synthetic load only, never quality evidence.
        passages = [r["content"] for r in experiment["corpora"]["semantic512"]["chunks"]]
        report["lexicalLoad"] = []
        query = cases[0]["question"]
        for factor in (1, 100, 1000):
            documents = tuple(passages * factor)
            index, build_ms = timed(lambda: LexicalIndex(documents))
            old_times, new_times = [], []
            for _ in range(5):
                def old():
                    model = BM25Okapi([tokenize(text) for text in documents])
                    scores = model.get_scores(tokenize(query))
                    return [(int(i), float(scores[i])) for i in np.argsort(-scores, kind="stable")[:12] if scores[i] > 0]
                old_hits, old_ms = timed(old)
                new_hits, new_ms = timed(lambda: index.search(query, 12))
                if [i for i, _ in old_hits] != [i for i, _ in new_hits]:
                    raise AssertionError("Postings ranking differs from BM25 reference")
                old_times.append(old_ms)
                new_times.append(new_ms)
            report["lexicalLoad"].append({"syntheticRepeatedCorpus": True, "documents": len(documents),
                                          "indexBuildMs": build_ms, "oldMedianMs": float(np.median(old_times)),
                                          "postingsMedianMs": float(np.median(new_times))})
        report["cache"] = lexical_index.cache_info()._asdict()
        report["sourceSHA256"] = {str(p.relative_to(ROOT)): fingerprint(p) for p in [
            Path(__file__), ROOT / "app/services/knowledge.py", ROOT / "app/services/chunking.py",
            ROOT / "app/services/lexical_index.py", ROOT / "app/models/entities.py",
        ]}
        report["status"] = "completed"
    except Exception as exc:
        report.update(status="failed", errorType=type(exc).__name__, error=str(exc))
        raise
    finally:
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        for engine in engines.values():
            engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--comparison", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.comparison.resolve(), args.output.resolve())
