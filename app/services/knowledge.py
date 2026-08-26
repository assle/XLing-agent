from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass

from pypdf import PdfReader
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.entities import KnowledgeChunk
from app.services.vector_store import FALLBACK_RETRIEVAL_LABEL, PRIMARY_RETRIEVAL_LABEL, ChromaKnowledgeStore


logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    chunk_id: int | None
    source: str
    content: str
    score: float


class KnowledgeService:
    def __init__(self, db: Session, settings: Settings, ai_client=None):
        self.db = db
        self.settings = settings
        self.vector_store = ChromaKnowledgeStore(settings)
        self.ai_client = ai_client
        self._bm25 = None

    def count(self) -> int:
        return self.db.query(KnowledgeChunk).count()

    def ensure_source(self, source: str, content: str) -> int:
        chunks = chunk_text(content, self.settings.knowledge_chunk_size, self.settings.knowledge_chunk_overlap)
        existing = [
            chunk.content
            for chunk in self.db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.source == source)
            .order_by(KnowledgeChunk.source_index.asc())
            .all()
        ]
        if existing == chunks:
            return len(existing)
        return self.ingest(source, content)

    def status(self) -> dict:
        vector_chunks = None
        vector_error = getattr(self.vector_store, "error", "")
        if self.vector_store.can_embed:
            try:
                vector_chunks = self.vector_store.count()
            except Exception as exc:
                vector_error = f"{type(exc).__name__}: {exc}"
        return {
            "retrievalOrder": [
                PRIMARY_RETRIEVAL_LABEL,
                f"{FALLBACK_RETRIEVAL_LABEL} when OPENAI_API_KEY/chromadb/vector call is unavailable",
            ],
            "primaryRetrieval": PRIMARY_RETRIEVAL_LABEL,
            "fallbackRetrieval": FALLBACK_RETRIEVAL_LABEL,
            "databaseChunks": self.count(),
            "vectorEnabled": self.settings.knowledge_vector_enabled,
            "vectorAvailable": self.vector_store.can_embed,
            "vectorRequired": self.settings.knowledge_vector_required,
            "embeddingModel": self.settings.openai_embedding_model,
            "vectorChunks": vector_chunks,
            "chromaPersistDir": self.settings.chroma_persist_dir,
            "chromaCollectionName": self.settings.chroma_collection_name,
            "chromaSnapshotDir": self.settings.chroma_snapshot_dir,
            "vectorError": vector_error,
        }

    def rebuild_vector_index(self) -> int:
        if not self.vector_store.can_embed:
            raise RuntimeError(getattr(self.vector_store, "error", "") or "Chroma 向量库不可用")
        rows = self.db.query(KnowledgeChunk).order_by(KnowledgeChunk.source.asc(), KnowledgeChunk.source_index.asc()).all()
        self._sync_vector_chunks(rows)
        self.db.commit()
        return len(rows)

    def backup_vector_index(self) -> str:
        if not self.vector_store.can_embed:
            raise RuntimeError(getattr(self.vector_store, "error", "") or "Chroma 向量库不可用")
        snapshot = self.vector_store.snapshot()
        if snapshot is None:
            raise RuntimeError("Chroma 持久化目录不存在，无法生成快照")
        return snapshot

    def ingest(self, source: str, content: str, exam_stage: str | None = None) -> int:
        chunks = chunk_text(content, self.settings.knowledge_chunk_size, self.settings.knowledge_chunk_overlap)
        self._delete_vector_source(source)
        self.db.query(KnowledgeChunk).filter(KnowledgeChunk.source == source).delete()
        rows = []
        for index, chunk in enumerate(chunks):
            row = KnowledgeChunk(source=source, source_index=index, content=chunk, exam_stage=exam_stage)
            self.db.add(row)
            rows.append(row)
        self.db.flush()
        self._index_vector_chunks(rows)
        self.db.commit()
        return len(chunks)

    def ingest_file(self, filename: str, data: bytes) -> int:
        lower = filename.lower()
        if lower.endswith(".pdf"):
            text = extract_pdf(data)
        else:
            text = data.decode("utf-8", errors="ignore")
        return self.ingest(filename, text)

    def retrieve(self, query: str, top_k: int | None = None) -> list[SearchResult]:
        top_k = top_k or self.settings.knowledge_top_k
        # Primary retrieval: embed the rewritten query with text-embedding-3-small,
        # then run Chroma nearest-neighbor search. Fallback happens only when
        # OPENAI_API_KEY/chromadb/vector calls are unavailable or fail.
        vector_results = self._retrieve_vector(query, top_k)
        if vector_results:
            return self._expand_best(vector_results, top_k)
        return self._retrieve_hybrid(query, top_k)

    def retrieve_multi_query(self, query: str, top_k: int | None = None, base_retrieve=None) -> list[SearchResult]:
        """Multi-query retrieval: generate 3 sub-queries, retrieve top-K for each,
        deduplicate by chunk_id, and return merged results.

        Issue 02: expands recall surface by querying the knowledge base from
        multiple angles. Falls back to single-query retrieve when no AI client
        is available.
        """
        top_k = top_k or self.settings.knowledge_top_k
        base_retrieve = base_retrieve or self.retrieve
        if not self.ai_client:
            return base_retrieve(query, top_k)
        sub_queries = self.ai_client.generate_sub_queries(query, n=3)
        if not sub_queries:
            sub_queries = [query]
        seen: dict[int | None, SearchResult] = {}
        for sq in sub_queries:
            for result in base_retrieve(sq, top_k):
                key = result.chunk_id
                if key not in seen or result.score > seen[key].score:
                    seen[key] = result
        merged = sorted(seen.values(), key=lambda r: r.score, reverse=True)
        return self._expand_best(merged[:top_k], top_k)

    def retrieve_hybrid_rrf(self, query: str, top_k: int | None = None) -> list[SearchResult]:
        """Hybrid retrieval with Reciprocal Rank Fusion (issue 03).

        Runs vector (or hybrid-score fallback) and BM25 retrieval in parallel,
        then fuses rankings with RRF (k=60). This replaces the serial fallback
        model: both channels contribute candidates regardless of vector availability.
        """
        top_k = top_k or self.settings.knowledge_top_k
        pool = top_k * 3
        vector_results = self._retrieve_vector(query, pool)
        if not vector_results:
            vector_results = self._retrieve_hybrid(query, pool)
        bm25_results = self._retrieve_bm25(query, pool)
        fused = self._rrf_fuse([vector_results, bm25_results], top_k)
        return self._expand_best(fused, top_k)

    def retrieve_with_rerank(self, query: str, top_k: int | None = None, candidate_pool: int = 20) -> list[SearchResult]:
        """LLM batch rerank (issue 04).

        Retrieves a larger candidate pool (default 20), then uses a single LLM
        call to batch-score all candidates. Returns top-K by LLM score.
        Falls back to unranked retrieval when no AI client is available.
        """
        top_k = top_k or self.settings.knowledge_top_k
        candidates = self.retrieve(query, candidate_pool)
        if not candidates or not self.ai_client:
            return candidates[:top_k] if candidates else []
        candidate_dicts = [
            {"source": c.source, "content": c.content, "chunk_id": c.chunk_id}
            for c in candidates
        ]
        scored = self.ai_client.rerank(query, candidate_dicts)
        index_to_score = {idx: score for idx, score in scored}
        reranked = []
        for idx, candidate in enumerate(candidates):
            score = index_to_score.get(idx, 0.0)
            reranked.append(SearchResult(candidate.chunk_id, candidate.source, candidate.content, score))
        reranked.sort(key=lambda r: r.score, reverse=True)
        return reranked[:top_k]


    def retrieve_with_stage(self, query: str, exam_stage: str | None = None, top_k: int | None = None) -> list[SearchResult]:
        """Retrieve with exam-stage filtering (issue 06).

        When exam_stage is provided, only chunks matching that stage
        (or with no/all stage metadata) are returned. When None, all
        chunks are searched (backward compatible).
        """
        top_k = top_k or self.settings.knowledge_top_k
        results = self.retrieve(query, top_k * 3 if exam_stage else top_k)
        if not exam_stage:
            return results[:top_k]
        filtered = [r for r in results if _matches_stage(self._get_chunk_stage(r.chunk_id), exam_stage)]
        return filtered[:top_k]

    def _get_chunk_stage(self, chunk_id: int | None) -> str | None:
        if chunk_id is None:
            return None
        chunk = self.db.get(KnowledgeChunk, chunk_id)
        return getattr(chunk, 'exam_stage', None) if chunk else None

    def _retrieve_bm25(self, query: str, top_k: int) -> list[SearchResult]:
        """BM25 keyword retrieval using rank_bm25."""
        chunks = self.db.query(KnowledgeChunk).order_by(
            KnowledgeChunk.source.asc(), KnowledgeChunk.source_index.asc()
        ).all()
        if not chunks:
            return []
        corpus = [tokenize(chunk.content) for chunk in chunks]
        try:
            from rank_bm25 import BM25Okapi
        except ImportError:
            logger.warning("rank_bm25 not installed; BM25 retrieval unavailable")
            return []
        bm25 = BM25Okapi(corpus)
        scores = bm25.get_scores(tokenize(query))
        ranked = sorted(enumerate(scores), key=lambda pair: pair[1], reverse=True)
        results = []
        for chunk_idx, score in ranked[:top_k]:
            if score <= 0:
                continue
            chunk = chunks[chunk_idx]
            results.append(SearchResult(chunk.id, chunk.source, chunk.content, float(score)))
        return results

    @staticmethod
    def _rrf_fuse(result_lists: list[list[SearchResult]], top_k: int, k: int = 60) -> list[SearchResult]:
        """Reciprocal Rank Fusion: score = sum(1 / (k + rank)) across lists."""
        scores: dict[int | None, float] = {}
        best: dict[int | None, SearchResult] = {}
        for result_list in result_lists:
            for rank, result in enumerate(result_list, start=1):
                key = result.chunk_id
                scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
                if key not in best:
                    best[key] = result
        fused = sorted(scores.keys(), key=lambda key: scores[key], reverse=True)
        return [
            SearchResult(best[key].chunk_id, best[key].source, best[key].content, scores[key])
            for key in fused[:top_k]
        ]

    def _retrieve_hybrid(self, query: str, top_k: int) -> list[SearchResult]:
        ranked = [
            SearchResult(chunk.id, chunk.source, chunk.content, hybrid_score(query, chunk.content))
            for chunk in self.db.query(KnowledgeChunk).all()
        ]
        ranked = [item for item in ranked if item.score > 0]
        ranked.sort(key=lambda item: item.score, reverse=True)
        return self._expand_best(ranked[:top_k], top_k)

    def _retrieve_vector(self, query: str, top_k: int) -> list[SearchResult]:
        if not self.vector_store.can_embed:
            return []
        try:
            self._ensure_vector_index()
            query_embedding = self.vector_store.embed_texts([query])[0]
            hits = self.vector_store.query(query_embedding, top_k)
        except Exception as exc:
            self._handle_vector_error("retrieve", exc)
            return []
        results = []
        for hit in hits:
            chunk = self.db.get(KnowledgeChunk, hit.chunk_id) if hit.chunk_id is not None else None
            results.append(
                SearchResult(
                    chunk.id if chunk is not None else hit.chunk_id,
                    chunk.source if chunk is not None else hit.source,
                    chunk.content if chunk is not None else hit.content,
                    hit.score,
                )
            )
        return results

    def _ensure_vector_index(self) -> None:
        rows = self.db.query(KnowledgeChunk).order_by(KnowledgeChunk.source.asc(), KnowledgeChunk.source_index.asc()).all()
        if not rows:
            return
        if (
            self.vector_store.count() == len(rows)
            and all(row.embedding_json for row in rows)
            and self.vector_store.has_exact_chunk_ids(rows)
        ):
            return
        self._sync_vector_chunks(rows)
        self.db.commit()

    def _delete_vector_source(self, source: str) -> None:
        if not self.vector_store.can_embed:
            return
        try:
            self.vector_store.delete_source(source)
        except Exception as exc:
            self._handle_vector_error("delete_source", exc)

    def _index_vector_chunks(self, chunks: list[KnowledgeChunk]) -> None:
        if not chunks or not self.vector_store.can_embed:
            return
        try:
            embeddings = self._embeddings_for_chunks(chunks)
            for chunk, embedding in zip(chunks, embeddings):
                chunk.embedding_json = json.dumps(embedding, separators=(",", ":"))
            self.vector_store.upsert_chunks(chunks, embeddings)
        except Exception as exc:
            self._handle_vector_error("index", exc)

    def _sync_vector_chunks(self, chunks: list[KnowledgeChunk]) -> None:
        if not chunks or not self.vector_store.can_embed:
            return
        try:
            embeddings = self._embeddings_for_chunks(chunks)
            for chunk, embedding in zip(chunks, embeddings):
                chunk.embedding_json = json.dumps(embedding, separators=(",", ":"))
            self.vector_store.sync_chunks(chunks, embeddings)
        except Exception as exc:
            self._handle_vector_error("sync", exc)

    def _embeddings_for_chunks(self, chunks: list[KnowledgeChunk]) -> list[list[float]]:
        embeddings: list[list[float] | None] = []
        missing_indexes = []
        missing_texts = []
        for index, chunk in enumerate(chunks):
            embedding = parse_embedding(chunk.embedding_json)
            embeddings.append(embedding)
            if embedding is None:
                missing_indexes.append(index)
                missing_texts.append(chunk.content)
        if missing_texts:
            new_embeddings = self.vector_store.embed_texts(missing_texts)
            for index, embedding in zip(missing_indexes, new_embeddings):
                embeddings[index] = embedding
        resolved = [embedding for embedding in embeddings if embedding is not None]
        if len(resolved) != len(chunks):
            raise ValueError("Embedding response count did not match knowledge chunks.")
        return resolved

    def _handle_vector_error(self, action: str, exc: Exception) -> None:
        if self.settings.knowledge_vector_required:
            raise exc
        logger.warning(
            "%s %s failed; falling back to %s: %s",
            PRIMARY_RETRIEVAL_LABEL,
            action,
            FALLBACK_RETRIEVAL_LABEL,
            exc,
        )

    def _expand_best(self, ranked: list[SearchResult], top_k: int) -> list[SearchResult]:
        if not ranked:
            return []
        best = ranked[0]
        expanded = self._expand(best)
        results = [expanded]
        for item in ranked[1:]:
            if item.chunk_id != expanded.chunk_id and len(results) < top_k:
                results.append(item)
        return results

    def _expand(self, result: SearchResult) -> SearchResult:
        if result.chunk_id is None:
            return result
        chunk = self.db.get(KnowledgeChunk, result.chunk_id)
        if chunk is None:
            return result
        neighbors = (
            self.db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.source == chunk.source)
            .filter(KnowledgeChunk.source_index >= max(0, chunk.source_index - 1))
            .filter(KnowledgeChunk.source_index <= chunk.source_index + 1)
            .order_by(KnowledgeChunk.source_index.asc())
            .all()
        )
        return SearchResult(chunk.id, chunk.source, "\n\n".join(item.content for item in neighbors), result.score)


def chunk_text(content: str, size: int, overlap: int) -> list[str]:
    text = re.sub(r"\s+", " ", content or "").strip()
    if not text:
        return []
    chunks = []
    start = 0
    step = max(1, size - overlap)
    while start < len(text):
        chunks.append(text[start:start + size])
        start += step
    return chunks


def hybrid_score(query: str, content: str) -> float:
    return token_cosine(query, content) * 0.75 + keyword_score(query, content) * 0.25


def parse_embedding(raw: str | None) -> list[float] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or not data:
        return None
    if not all(isinstance(item, (int, float)) for item in data):
        return None
    return [float(item) for item in data]


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]", text.lower())
    grams = words[:]
    compact = "".join(ch for ch in text.lower() if "\u4e00" <= ch <= "\u9fff")
    grams.extend(compact[i:i + 2] for i in range(max(0, len(compact) - 1)))
    return [item for item in grams if item.strip()]


def token_cosine(left: str, right: str) -> float:
    left_counts = counts(tokenize(left))
    right_counts = counts(tokenize(right))
    if not left_counts or not right_counts:
        return 0.0
    dot = sum(value * right_counts.get(key, 0) for key, value in left_counts.items())
    left_norm = math.sqrt(sum(value * value for value in left_counts.values()))
    right_norm = math.sqrt(sum(value * value for value in right_counts.values()))
    return 0.0 if left_norm == 0 or right_norm == 0 else dot / (left_norm * right_norm)


def keyword_score(query: str, content: str) -> float:
    terms = [term for term in re.split(r"[\s，。！？、；：,.!?;:]+", query.lower()) if len(term) >= 2]
    if not terms:
        return 0.0
    lower = content.lower()
    matched = sum(1 for term in terms if term in lower)
    return min(1.0, matched / len(terms))


def counts(values: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return result


def extract_pdf(data: bytes) -> str:
    from io import BytesIO

    reader = PdfReader(BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _matches_stage(chunk_stage: str | None, target_stage: str) -> bool:
    """Check if a chunk's exam stage matches the target stage.

    - No stage metadata (None) means all stages -> always matches
    - "all" means all stages -> always matches
    - Comma-separated stages -> match if target is in the list
    """
    if chunk_stage is None or chunk_stage == "" or chunk_stage == "all":
        return True
    stages = [s.strip() for s in chunk_stage.split(",")]
    return target_stage in stages
