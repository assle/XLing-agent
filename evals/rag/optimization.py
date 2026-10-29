"""Controlled chunking/fusion experiment using real, content-addressed embeddings.

Leave-one-topic-out selection is reported on an already public, reviewed benchmark.
This is internal cross-validation, not an unseen test. No generated relevance labels.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
from rank_bm25 import BM25Okapi

from app.core.config import Settings
from app.services.chunking import chunk_text, semantic_chunk_text
from app.services.knowledge import SearchResult, hybrid_score, normalized_fusion, tokenize
from app.services.vector_store import ChromaKnowledgeStore
from evals.rag.closing import ROOT, evidence_matches, fingerprint, load_cases, require_review, score_case, summarize

TOP_K = 4
CONTEXT_BUDGET = 3072


def fixed_chunks(text: str, size: int = 512, overlap: int = 64) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    return [text[i:i + size] for i in range(0, len(text), size - overlap)]


class Embeddings:
    def __init__(self, store: ChromaKnowledgeStore, directory: Path):
        self.store = store
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.network_ms: list[float] = []

    def get(self, texts: list[str]) -> np.ndarray:
        identity = (self.store.settings.embedding_base_url or self.store.settings.openai_base_url,
                    self.store.settings.openai_embedding_model)
        paths = [self.directory / (hashlib.sha256(json.dumps([identity, text]).encode()).hexdigest() + ".json")
                 for text in texts]
        missing = list(dict.fromkeys(i for i, path in enumerate(paths) if not path.exists()))
        for start in range(0, len(missing), 10):
            indexes = missing[start:start + 10]
            tick = time.perf_counter()
            vectors = self.store.embed_texts([texts[i] for i in indexes])
            self.network_ms.append((time.perf_counter() - tick) * 1000)
            for i, vector in zip(indexes, vectors, strict=True):
                paths[i].write_text(json.dumps(vector))
            print(json.dumps({"embedded": min(start + 10, len(missing)), "total": len(missing)}), flush=True)
        return np.asarray([json.loads(path.read_text()) for path in paths], dtype=np.float32)


def expand(ranked: list[SearchResult], corpus: list[SearchResult], *, all_hits: bool = False) -> list[SearchResult]:
    positions = {row.chunk_id: i for i, row in enumerate(corpus)}
    results = []
    consumed: set[int] = set()
    remaining = CONTEXT_BUDGET
    for rank, row in enumerate(ranked):
        if row.chunk_id in consumed:
            continue
        center = positions[row.chunk_id]
        neighbors = [corpus[i] for i in range(max(0, center - 1), min(len(corpus), center + 2))
                     if corpus[i].source == row.source] if (rank == 0 or all_hits) else [row]
        # Do not duplicate a passage already present in another returned window.
        if all_hits:
            neighbors = [item for item in neighbors if item.chunk_id not in consumed]
        content = "\n\n".join(item.content for item in neighbors)
        if len(content) > remaining:
            content = row.content[:remaining]
            neighbors = [row]
        if not content:
            break
        results.append(SearchResult(row.chunk_id, row.source, content, row.score))
        remaining -= len(content)
        if all_hits:
            consumed.update(item.chunk_id for item in neighbors)
        if len(results) == TOP_K:
            break
    return results


def fuse(dense: list[SearchResult], sparse: list[SearchResult], method: str,
         corpus: list[SearchResult], vectors: np.ndarray, query: np.ndarray,
         sparse_scores: np.ndarray) -> list[SearchResult]:
    if method == "dense":
        return dense
    if method.startswith("convex"):
        alpha = int(method.removeprefix("convex")) / 100
        ids = sorted({row.chunk_id for row in dense + sparse})
        # Complement both scores over the candidate union (Bruch et al., section 3.1).
        cosine = vectors[ids] @ query / np.maximum(np.linalg.norm(vectors[ids], axis=1) * np.linalg.norm(query), 1e-12)
        lexical = sparse_scores[ids]
        combined = normalized_fusion(cosine, lexical, alpha)
        scores = dict(zip(ids, combined.tolist(), strict=True))
    else:
        scores = {}
        for ranking in [dense, sparse]:
            for rank, row in enumerate(ranking, 1):
                scores[row.chunk_id] = scores.get(row.chunk_id, 0.0) + 1 / (60 + rank)
    return [SearchResult(key, corpus[key].source, corpus[key].content, scores[key])
            for key in sorted(scores, key=lambda key: (-scores[key], key))]


def measure(case: dict, found: list[SearchResult], latency_ms: float) -> dict:
    result = score_case(case, found, TOP_K)
    evidence = case["expectedEvidence"]
    result["evidenceRecall"] = (sum(any(evidence_matches(row, e) for row in found) for e in evidence)
                                / len(evidence)) if evidence else None
    result["contextChars"] = sum(len(row.content) for row in found)
    result["latencyMs"] = latency_ms
    # Frozen corpus and deterministic expansion make full text redundant in every arm.
    for row in result["retrieved"]:
        row["contentSHA256"] = hashlib.sha256(row.pop("content").encode()).hexdigest()
    return result


def summary(rows: list[dict]) -> dict:
    result = summarize(rows)
    result["evidenceRecallAt4"] = float(np.mean([r["evidenceRecall"] for r in rows if r["answerable"]]))
    result["meanContextChars"] = float(np.mean([r["contextChars"] for r in rows]))
    return result


def run(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "comparison.json"
    if report_path.exists():
        raise FileExistsError("Choose a fresh output directory; embedding cache may be reused")
    dataset = ROOT / "evals/rag/closing-40.jsonl"
    review_path = ROOT / "evals/rag/closing-gold-review.json"
    files = sorted((ROOT / "app/knowledge").glob("*.md")) + [ROOT / "evals/rag/closing-deployment-resources.md"]
    sources = {p.name: fingerprint(p) for p in files}
    cases = load_cases(dataset)
    require_review(json.loads(review_path.read_text()), cases, fingerprint(dataset), sources)
    settings = Settings(chroma_persist_dir=str(output / "chroma"), chroma_collection_name="experiment_probe",
                        knowledge_vector_required=True, knowledge_vector_enabled=True)
    store = ChromaKnowledgeStore(settings)
    embeddings = Embeddings(store, output.parent / "embedding-cache")
    report = {
        "status": "preparing", "datasetSHA256": fingerprint(dataset), "knowledgeSourceSHA256": sources,
        "reviewSHA256": fingerprint(review_path), "embeddingModel": settings.openai_embedding_model,
        "embeddingHost": urlsplit(settings.embedding_base_url or settings.openai_base_url).hostname,
        "protocol": {
            "topK": TOP_K, "contextBudgetChars": CONTEXT_BUDGET, "candidatePool": 12,
            "folds": "leave one topic out; train selects configuration, held-out topic only scores it",
            "selection": "hybrid arms only: min safety misses, max evidence recall, max MRR, min context chars, lexicographic name",
            "timing": "cached real query embeddings; Chroma query + cached BM25 scoring + fusion + expansion; no HTTP/SQL",
            "evidence": "human-reviewed engineering questions, real embeddings; not clinical or unseen-domain validation",
            "methods": [f"{chunk}-{fusion}" for chunk in ["fixed512", "structure512", "structure256", "semantic512", "parent256"]
                        for fusion in ["dense", "rrf", "convex50", "convex75", "convex90", "convex95", "convex98"]],
        }, "corpora": {}, "arms": {},
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    queries = embeddings.get([c["question"] for c in cases])
    query_vectors = dict(zip([c["id"] for c in cases], queries, strict=True))
    indexes = {}
    for name in ["fixed512", "structure512", "structure256", "semantic512", "parent256"]:
        tick = time.perf_counter()
        corpus = []
        for path in files:
            text = path.read_text()
            chunks = (fixed_chunks(text) if name == "fixed512" else
                      semantic_chunk_text(text, 512, 64, embeddings.get) if name == "semantic512" else
                      chunk_text(text, 256 if name in {"structure256", "parent256"} else 512, 64))
            corpus.extend([SearchResult(None, path.name, chunk, 0.0) for chunk in chunks])
        corpus = [SearchResult(i, row.source, row.content, 0.0) for i, row in enumerate(corpus)]
        vectors = embeddings.get([r.content for r in corpus])
        collection = store.client.get_or_create_collection(name="experiment_" + name,
                                                            embedding_function=None, metadata={"hnsw:space": "cosine"})
        collection.upsert(ids=[str(r.chunk_id) for r in corpus], embeddings=vectors.tolist())
        sparse = BM25Okapi([tokenize(r.content) for r in corpus])
        indexes[name] = (corpus, collection, sparse, vectors)
        report["corpora"][name] = {"chunks": [r.__dict__ for r in corpus], "indexBuildMs": (time.perf_counter()-tick)*1000,
                                    "embeddingBytes": vectors.nbytes}
        print(json.dumps({"indexed": name, "chunks": len(corpus)}), flush=True)

    def evaluate(name: str, selected_cases: list[dict]) -> list[dict]:
        chunk_name, method = name.split("-")
        corpus, collection, sparse, vectors = indexes[chunk_name]
        rows = []
        for case in selected_cases:
            # One warm-up then three identical local repetitions, reporting the median.
            times = []
            for _ in range(4):
                tick = time.perf_counter()
                if method == "local":
                    ranked = sorted([SearchResult(r.chunk_id, r.source, r.content, hybrid_score(case["question"], r.content))
                                     for r in corpus], key=lambda r: r.score, reverse=True)
                    ranked = [r for r in ranked if r.score > 0][:TOP_K]
                else:
                    hits = collection.query(query_embeddings=[query_vectors[case["id"]].tolist()],
                                            n_results=min(12, len(corpus)), include=["distances"])
                    dense = [SearchResult(int(key), corpus[int(key)].source, corpus[int(key)].content, 1 / (1 + max(0, d)))
                             for key, d in zip(hits["ids"][0], hits["distances"][0], strict=True)]
                    scores = sparse.get_scores(tokenize(case["question"])) if method != "dense" else []
                    lexical = [SearchResult(int(i), corpus[i].source, corpus[i].content, float(scores[i]))
                               for i in np.argsort(-np.asarray(scores), kind="stable")[:12] if scores[i] > 0]
                    ranked = fuse(dense, lexical, method, corpus, vectors, query_vectors[case["id"]], np.asarray(scores))
                found = expand(ranked[:12 if chunk_name == "parent256" else TOP_K], corpus,
                               all_hits=chunk_name == "parent256")
                times.append((time.perf_counter() - tick) * 1000)
            rows.append(measure(case, found, float(np.median(times[1:]))))
        return rows

    for name in [*report["protocol"]["methods"], "fixed512-local"]:
        rows = evaluate(name, cases)
        report["arms"][name] = {"summary": summary(rows), "cases": rows}
        print(json.dumps({"arm": name, **summary(rows)}), flush=True)
    hybrids = [name for name in report["arms"] if "-convex" in name or name.endswith("-rrf")]
    def select(ids: set[str]) -> str:
        def objective(name: str) -> tuple:
            rows = [r for r in report["arms"][name]["cases"] if r["id"] in ids]
            s = summary(rows)
            return (s["safetyEvidenceMissRate"] or 0, -s["evidenceRecallAt4"], -s["mrr"], s["meanContextChars"], name)
        return min(hybrids, key=objective)
    folds = []
    held_out = []
    for topic in sorted({c["topic"] for c in cases}):
        train = {c["id"] for c in cases if c["topic"] != topic}
        test = {c["id"] for c in cases if c["topic"] == topic}
        winner = select(train)
        rows = [r for r in report["arms"][winner]["cases"] if r["id"] in test]
        folds.append({"topic": topic, "selectedOnOtherTopics": winner, "testIDs": sorted(test), "summary": summary(rows)})
        held_out.extend(rows)
    report["crossValidation"] = {"folds": folds, "summary": summary(held_out), "cases": held_out}
    report["selected"] = select({c["id"] for c in cases})
    report["newEmbeddingRequestsMs"] = embeddings.network_ms
    report["sourceSHA256"] = {str(p.relative_to(ROOT)): fingerprint(p) for p in [Path(__file__), ROOT / "app/services/chunking.py"]}
    report["status"] = "completed"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.output.resolve())
