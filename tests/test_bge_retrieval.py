from __future__ import annotations

import numpy as np

from app.core.config import Settings
from app.models.entities import KnowledgeChunk
from app.services.bge_retrieval import BgeM3Retriever, FlagEmbeddingBackend
from app.services.knowledge import KnowledgeService
from tests.support import DatabaseHarness


class FakeBgeBackend:
    model_name = "fake-bge-m3"
    reranker_name = "fake-reranker"
    index_size_bytes = 2048

    def score(self, query: str, documents: list[str]) -> list[float]:
        return [0.2 if "睡眠" in document else 0.9 for document in documents]

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        return [0.95 if "睡眠" in document else 0.1 for document in documents]


class BrokenBgeBackend(FakeBgeBackend):
    def score(self, query: str, documents: list[str]) -> list[float]:
        raise RuntimeError("device unavailable")


def test_knowledge_retrieve_uses_bge_candidates_then_dedicated_reranker():
    harness = DatabaseHarness()
    db = harness.sessions()
    try:
        db.add_all([
            KnowledgeChunk(source="anxiety.md", source_index=0, content="考试焦虑时先拆分任务"),
            KnowledgeChunk(source="sleep.md", source_index=0, content="失眠时固定起床时间改善睡眠"),
        ])
        db.commit()
        retriever = BgeM3Retriever(FakeBgeBackend(), candidate_pool=2, rerank=True)
        knowledge = KnowledgeService(
            db,
            Settings(knowledge_vector_enabled=False),
            retriever=retriever,
        )

        results = knowledge.retrieve("失眠怎么办", top_k=2)

        assert [result.source for result in results] == ["sleep.md", "anxiety.md"]
        assert results[0].score == 0.95
        assert retriever.embedding_model == "fake-bge-m3"
        assert retriever.reranker_model == "fake-reranker"
    finally:
        db.close()
        harness.close()


def test_optional_bge_runtime_error_falls_back_to_current_retriever():
    harness = DatabaseHarness()
    db = harness.sessions()
    try:
        db.add(
            KnowledgeChunk(
                source="sleep.md",
                source_index=0,
                content="失眠时固定起床时间改善睡眠",
            )
        )
        db.commit()
        knowledge = KnowledgeService(
            db,
            Settings(knowledge_vector_enabled=False, bge_required=False),
            retriever=BgeM3Retriever(BrokenBgeBackend(), rerank=False),
        )

        results = knowledge.retrieve("失眠怎么办", top_k=1)

        assert results[0].source == "sleep.md"
    finally:
        db.close()
        harness.close()


def test_bge_document_encoding_is_shared_across_request_scoped_backends():
    class Model:
        def __init__(self):
            self.batch_sizes = []

        def encode(self, texts, **kwargs):
            self.batch_sizes.append(len(texts))
            return {
                "dense_vecs": np.ones((len(texts), 2), dtype=np.float32),
                "lexical_weights": [{"token": 1.0} for _ in texts],
            }

        def compute_lexical_matching_score(self, query, document):
            return 1.0

    key = ("cache-test-model", False, "cpu")
    model = Model()
    FlagEmbeddingBackend._model_cache[key] = model
    FlagEmbeddingBackend._document_cache_by_model.pop(key, None)
    try:
        first = FlagEmbeddingBackend(
            "cache-test-model",
            "unused-reranker",
            use_fp16=False,
            device="cpu",
        )
        second = FlagEmbeddingBackend(
            "cache-test-model",
            "unused-reranker",
            use_fp16=False,
            device="cpu",
        )

        first.score("query one", ["doc one", "doc two"])
        second.score("query two", ["doc one", "doc two"])

        assert model.batch_sizes == [1, 2, 1]
        assert second.index_size_bytes > 0
    finally:
        FlagEmbeddingBackend._model_cache.pop(key, None)
        FlagEmbeddingBackend._document_cache_by_model.pop(key, None)
