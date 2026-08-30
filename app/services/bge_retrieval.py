from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from app.core.config import Settings
from app.models.entities import KnowledgeChunk


class BgeRetrieverUnavailable(RuntimeError):
    pass


class BgeBackend(Protocol):
    model_name: str
    reranker_name: str
    index_size_bytes: int

    def score(self, query: str, documents: list[str]) -> list[float]: ...

    def rerank(self, query: str, documents: list[str]) -> list[float]: ...


@dataclass(frozen=True)
class RankedKnowledgeChunk:
    chunk: KnowledgeChunk
    score: float


class BgeM3Retriever:
    def __init__(
        self,
        backend: BgeBackend,
        *,
        candidate_pool: int = 20,
        rerank: bool = True,
    ) -> None:
        self.backend = backend
        self.candidate_pool = max(1, candidate_pool)
        self.rerank_enabled = rerank
        self.index_size_bytes = 0
        self.corpus_size_bytes = 0

    @classmethod
    def from_settings(cls, settings: Settings) -> BgeM3Retriever:
        return cls(
            FlagEmbeddingBackend(
                settings.bge_embedding_model,
                settings.bge_reranker_model,
                use_fp16=settings.bge_use_fp16,
                device=settings.bge_device,
            ),
            candidate_pool=settings.bge_candidate_pool,
            rerank=settings.bge_rerank_enabled,
        )

    @property
    def embedding_model(self) -> str:
        return self.backend.model_name

    @property
    def reranker_model(self) -> str:
        return self.backend.reranker_name if self.rerank_enabled else ""

    def retrieve(
        self,
        query: str,
        chunks: list[KnowledgeChunk],
        top_k: int,
    ) -> list[RankedKnowledgeChunk]:
        rows = [chunk for chunk in chunks if chunk.content.strip()]
        if not rows:
            return []
        self.corpus_size_bytes = sum(len(chunk.content.encode("utf-8")) for chunk in rows)
        try:
            scores = self.backend.score(query, [chunk.content for chunk in rows])
        except BgeRetrieverUnavailable:
            raise
        except Exception as exc:
            raise BgeRetrieverUnavailable(
                f"BGE-M3 检索失败：{type(exc).__name__}"
            ) from exc
        if len(scores) != len(rows):
            raise BgeRetrieverUnavailable("BGE-M3 返回的候选分数数量不匹配")
        self.index_size_bytes = int(getattr(self.backend, "index_size_bytes", 0))
        candidates = sorted(
            zip(rows, scores, strict=True),
            key=lambda item: item[1],
            reverse=True,
        )[: max(top_k, self.candidate_pool)]
        if self.rerank_enabled and candidates:
            try:
                rerank_scores = self.backend.rerank(
                    query,
                    [chunk.content for chunk, _ in candidates],
                )
            except BgeRetrieverUnavailable:
                raise
            except Exception as exc:
                raise BgeRetrieverUnavailable(
                    f"BGE 重排失败：{type(exc).__name__}"
                ) from exc
            if len(rerank_scores) != len(candidates):
                raise BgeRetrieverUnavailable("BGE 重排分数数量不匹配")
            candidates = [
                (chunk, score)
                for (chunk, _), score in zip(candidates, rerank_scores, strict=True)
            ]
            candidates.sort(key=lambda item: item[1], reverse=True)
        return [
            RankedKnowledgeChunk(chunk=chunk, score=float(score))
            for chunk, score in candidates[:top_k]
        ]


class FlagEmbeddingBackend:
    """Lazy official FlagEmbedding adapter so the default app stays lightweight."""

    _model_cache: ClassVar[dict[tuple[str, bool, str], Any]] = {}
    _reranker_cache: ClassVar[dict[tuple[str, bool, str], Any]] = {}
    _document_cache_by_model: ClassVar[
        dict[tuple[str, bool, str], tuple[tuple[str, ...], Any, Any, int]]
    ] = {}
    _inference_lock: ClassVar[threading.RLock] = threading.RLock()

    def __init__(
        self,
        model_name: str,
        reranker_name: str,
        *,
        use_fp16: bool,
        device: str,
    ) -> None:
        self.model_name = model_name
        self.reranker_name = reranker_name
        self.use_fp16 = use_fp16
        self.device = device
        self.index_size_bytes = 0

    def score(self, query: str, documents: list[str]) -> list[float]:
        with self._inference_lock:
            model = self._embedding_model()
            query_encoded = model.encode(
                [query],
                return_dense=True,
                return_sparse=True,
                return_colbert_vecs=False,
            )
            document_key = tuple(documents)
            model_key = (self.model_name, self.use_fp16, self.device)
            cached = self._document_cache_by_model.get(model_key)
            if cached is None or cached[0] != document_key:
                document_encoded = model.encode(
                    documents,
                    return_dense=True,
                    return_sparse=True,
                    return_colbert_vecs=False,
                )
                index_size = _encoded_index_size(
                    document_encoded["dense_vecs"],
                    document_encoded["lexical_weights"],
                )
                cached = (
                    document_key,
                    document_encoded["dense_vecs"],
                    document_encoded["lexical_weights"],
                    index_size,
                )
                self._document_cache_by_model[model_key] = cached
            _, dense_vectors, lexical_weights, index_size = cached
            self.index_size_bytes = index_size
            query_dense = query_encoded["dense_vecs"][0]
            query_sparse = query_encoded["lexical_weights"][0]
            scores = []
            for dense, sparse in zip(dense_vectors, lexical_weights, strict=True):
                dense_score = float(query_dense @ dense)
                sparse_score = float(
                    model.compute_lexical_matching_score(query_sparse, sparse)
                )
                scores.append(dense_score + sparse_score)
            return scores

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        with self._inference_lock:
            reranker = self._reranker_model()
            scores = reranker.compute_score(
                [[query, document] for document in documents],
                normalize=True,
            )
        if isinstance(scores, (int, float)):
            return [float(scores)]
        return [float(score) for score in scores]

    def _embedding_model(self):
        key = (self.model_name, self.use_fp16, self.device)
        if key not in self._model_cache:
            try:
                from FlagEmbedding import BGEM3FlagModel
            except ImportError as exc:
                raise BgeRetrieverUnavailable(
                    "缺少 FlagEmbedding；请安装 requirements-bge.txt"
                ) from exc
            self._model_cache[key] = BGEM3FlagModel(
                self.model_name,
                use_fp16=self.use_fp16,
                devices=self.device,
            )
        return self._model_cache[key]

    def _reranker_model(self):
        key = (self.reranker_name, self.use_fp16, self.device)
        if key not in self._reranker_cache:
            try:
                from FlagEmbedding import FlagReranker
            except ImportError as exc:
                raise BgeRetrieverUnavailable(
                    "缺少 FlagEmbedding；请安装 requirements-bge.txt"
                ) from exc
            self._reranker_cache[key] = FlagReranker(
                self.reranker_name,
                use_fp16=self.use_fp16,
                devices=self.device,
            )
        return self._reranker_cache[key]


def _encoded_index_size(dense_vectors: Any, lexical_weights: Any) -> int:
    dense_size = int(getattr(dense_vectors, "nbytes", 0))
    sparse_size = sum(
        len(str(token).encode("utf-8")) + 8
        for weights in lexical_weights
        for token in weights
    )
    return dense_size + sparse_size
