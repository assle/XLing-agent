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

    def score(self, query: str, documents: list[str]) -> list[float]:
        """约定检索后端应为查询与每篇文档返回一个分数。

        返回列表须与 documents 顺序及长度一致；此处只是接口约定，不提供具体计算。
        """
        ...

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        """约定重排后端应重新评估查询与候选文档的相关性。

        返回分数按传入文档顺序对应；具体模型实现由后端提供。
        """
        ...


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
        """配置离线检索后端、候选数量和是否再次排序。

        候选数量至少为一；索引和语料大小统计先初始化为零。
        """
        self.backend = backend
        self.candidate_pool = max(1, candidate_pool)
        self.rerank_enabled = rerank
        self.index_size_bytes = 0
        self.corpus_size_bytes = 0

    @classmethod
    def from_settings(cls, settings: Settings) -> BgeM3Retriever:
        """根据配置构造延迟加载的嵌入与重排后端。

        模型名称、计算精度和设备均从 settings 读取，模型在首次评分时实际加载。
        """
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
        """返回检索后端所用嵌入模型名称。

        仅用于状态和评估记录，不触发模型加载。
        """
        return self.backend.model_name

    @property
    def reranker_model(self) -> str:
        """返回启用的重排模型名称。

        未启用重排时返回空字符串，使评估记录明确区分有无重排。
        """
        return self.backend.reranker_name if self.rerank_enabled else ""

    def retrieve(
        self,
        query: str,
        chunks: list[KnowledgeChunk],
        top_k: int,
    ) -> list[RankedKnowledgeChunk]:
        """过滤空片段，执行初次评分并按配置对候选再次排序。

        检查每次评分数量与文档数量一致，避免错配；返回最多 top_k 个带分数的原片段。
        后端异常统一包装为检索不可用错误，便于离线评估识别回退原因。
        """
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
        # 评分必须与有效文档逐一对应，数量不符时停止而不是截断配对。
        if len(scores) != len(rows):
            raise BgeRetrieverUnavailable("BGE-M3 返回的候选分数数量不匹配")
        self.index_size_bytes = int(getattr(self.backend, "index_size_bytes", 0))
        candidates = sorted(
            zip(rows, scores, strict=True),
            # 取候选二元组中的分数作为排序依据，分数较高者优先。
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
            # 取候选二元组中的分数作为排序依据，分数较高者优先。
            candidates.sort(key=lambda item: item[1], reverse=True)
        return [
            RankedKnowledgeChunk(chunk=chunk, score=float(score))
            for chunk, score in candidates[:top_k]
        ]


class FlagEmbeddingBackend:
    """按需加载官方模型实现，为离线检索提供可复用的评分和重排能力。"""

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
        """保存模型名称、设备及精度配置，不立即加载模型。

        类级缓存供多个实例复用，减少重复加载的内存和时间消耗。
        """
        self.model_name = model_name
        self.reranker_name = reranker_name
        self.use_fp16 = use_fp16
        self.device = device
        self.index_size_bytes = 0

    def score(self, query: str, documents: list[str]) -> list[float]:
        """将查询和文档编码后，把稠密相似度与词项匹配分数相加。

        同一模型配置且文档序列未变时复用文档编码；每次重新编码查询。
        共享锁保护模型推理和缓存更新，防止并发请求同时修改共享资源。
        """
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
            # 只有模型配置相同且文档文本和顺序都一致时才能复用编码。
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
                # 把语义向量与词项匹配两路分数直接相加，得到此后端的初始排序依据。
                scores.append(dense_score + sparse_score)
            return scores

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        """用专用模型为查询和每个候选文档重新评分。

        推理在共享锁内完成；单条结果可能为标量，统一转换成浮点列表返回。
        """
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
        """按模型、精度和设备组合查找或加载嵌入模型。

        缺少依赖时抛出明确的检索不可用错误，已加载实例保留在进程缓存。
        """
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
        """按配置查找或加载专用重排模型。

        与嵌入模型使用独立缓存；只有真正请求重排时才需要该模型。
        """
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
    """估算稠密向量与稀疏词权重的存储字节数。

    稠密部分读取 nbytes，稀疏部分按词项文本长度加八字节估计；不等于整个进程内存或实际文件大小。
    """
    dense_size = int(getattr(dense_vectors, "nbytes", 0))
    sparse_size = sum(
        len(str(token).encode("utf-8")) + 8
        for weights in lexical_weights
        for token in weights
    )
    return dense_size + sparse_size
