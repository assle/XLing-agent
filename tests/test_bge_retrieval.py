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
        """为含睡眠的文档给较低初始分，其余给较高分。

        故意让初排与后续重排相反，便于测试观察顺序变化。
        """
        return [0.2 if "睡眠" in document else 0.9 for document in documents]

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        """为含睡眠的候选给更高重排分。

        用于确认最终顺序确实采用专用重排结果。
        """
        return [0.95 if "睡眠" in document else 0.1 for document in documents]


class BrokenBgeBackend(FakeBgeBackend):
    def score(self, query: str, documents: list[str]) -> list[float]:
        """模拟设备不可用导致评分失败。

        供知识服务测试回到当前默认检索路径。
        """
        raise RuntimeError("device unavailable")


def test_knowledge_retrieve_uses_bge_candidates_then_dedicated_reranker():
    """保存两篇知识并注入可控初排和重排后端。

    检查最终睡眠文档排首位，分数及记录的模型名称准确。
    """
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
    """让实验后端评分异常，并关闭默认向量依赖。

    检查本地备用检索仍能找到睡眠资料。
    """
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
    """在两个后端实例中用不同查询检索同一文档列表。

    检查文档仅编码一次、查询各编码一次，并记录非零索引大小。
    """
    class Model:
        def __init__(self):
            """准备记录每次编码批量大小的列表。

            用于分辨查询编码和文档编码次数。
            """
            self.batch_sizes = []

        def encode(self, texts, **kwargs):
            """记录传入文本数量并返回形状一致的固定向量和词权重。

            模拟模型编码，不加载真实参数。
            """
            self.batch_sizes.append(len(texts))
            return {
                "dense_vecs": np.ones((len(texts), 2), dtype=np.float32),
                "lexical_weights": [{"token": 1.0} for _ in texts],
            }

        def compute_lexical_matching_score(self, query, document):
            """固定返回词项匹配分数一。

            让缓存测试不依赖真实评分算法。
            """
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
