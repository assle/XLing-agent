from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from app.core import diagnostics
from app.core.config import Settings
from app.models.entities import KnowledgeChunk

PRIMARY_RETRIEVAL_LABEL = "Chroma + OpenAI text-embedding-3-small"
FALLBACK_RETRIEVAL_LABEL = "local hybrid_score"
# DashScope 的同步兼容接口按模型限制为 10、20 或 25 条；10 条也适用于 OpenAI。
_EMBEDDING_BATCH_SIZE = 10


class VectorStoreUnavailable(RuntimeError):
    pass


@dataclass
class VectorSearchHit:
    chunk_id: int | None
    source: str
    source_index: int
    content: str
    score: float


class ChromaKnowledgeStore:
    """保存远程生成的文本向量，并使用 Chroma 向量库检索相近知识片段。"""

    def __init__(self, settings: Settings):
        """根据配置准备持久化向量集合，并记录不可用原因。

        未启用时直接返回；缺少密钥或依赖时按强制开关决定报错或允许备用检索。
        创建集合会访问本地索引目录，但尚未验证远程嵌入接口可调用。
        """
        self.settings = settings
        self.can_embed = False
        self.error = ""
        if not settings.knowledge_vector_enabled:
            self.error = "Chroma 向量库未启用"
            return
        embedding_key = settings.embedding_api_key or settings.openai_api_key
        if not embedding_key:
            if settings.knowledge_vector_required:
                raise VectorStoreUnavailable("缺少 EMBEDDING_API_KEY 或 OPENAI_API_KEY，无法启用 embedding 主检索方案")
            self.error = "缺少 EMBEDDING_API_KEY 或 OPENAI_API_KEY，embedding 不可用，已回退到本地 hybrid_score 检索"
            return
        try:
            import chromadb
        except ImportError as exc:
            if settings.knowledge_vector_required:
                raise VectorStoreUnavailable("缺少 chromadb 依赖，无法启用 Chroma + text-embedding-3-small 主检索方案") from exc
            self.error = "缺少 chromadb 依赖，Chroma + text-embedding-3-small 不可用，已回退到本地 hybrid_score 检索"
            return

        persist_dir = self._resolve_path(settings.chroma_persist_dir)
        persist_dir.mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(path=str(persist_dir))
        self.collection = self.client.get_or_create_collection(
            name=settings.chroma_collection_name,
            embedding_function=None,
            metadata={"hnsw:space": "cosine", "embedding_model": settings.openai_embedding_model},
        )
        self.can_embed = settings.knowledge_vector_enabled

    def upsert_chunks(self, chunks: list[KnowledgeChunk], embeddings: list[list[float]]) -> int:
        """向集合新增或更新已有数据库编号的非空片段。

        embeddings 应与筛选后的有效行顺序一致；返回写入行数，不提交业务数据库。
        """
        rows = [chunk for chunk in chunks if chunk.id is not None and chunk.content.strip()]
        if not rows:
            return 0
        ids = [self._id(chunk.id) for chunk in rows]
        documents = [chunk.content for chunk in rows]
        metadatas = [
            {"db_id": int(chunk.id), "source": chunk.source, "source_index": int(chunk.source_index)}
            for chunk in rows
        ]
        self.collection.upsert(
            ids=ids,
            documents=documents,
            metadatas=metadatas,  # type: ignore[arg-type]
            embeddings=embeddings,  # type: ignore[arg-type]
        )
        return len(rows)

    def sync_chunks(self, chunks: list[KnowledgeChunk], embeddings: list[list[float]]) -> int:
        """删除当前数据库片段集合之外的过时索引，再写入有效片段。

        以稳定的片段编号比较集合，确保旧数据不会残留在检索结果中。
        """
        valid_ids = {self._id(int(chunk.id)) for chunk in chunks if chunk.id is not None}
        current_ids = set(self.collection.get().get("ids", []))
        stale_ids = sorted(current_ids - valid_ids)
        if stale_ids:
            self.collection.delete(ids=stale_ids)
        return self.upsert_chunks(chunks, embeddings)

    def has_exact_chunk_ids(self, chunks: list[KnowledgeChunk]) -> bool:
        """比较向量集合与数据库片段的编号是否完全一致。

        既检查缺失编号也检查多余编号，不只比较数量。
        """
        valid_ids = {self._id(int(chunk.id)) for chunk in chunks if chunk.id is not None}
        current_ids = set(self.collection.get().get("ids", []))
        return current_ids == valid_ids

    def delete_source(self, source: str) -> None:
        """删除向量集合中具有指定来源标记的全部内容。

        组件未启用时直接返回；实际删除错误由上层处理。
        """
        if not self.can_embed:
            return
        self.collection.delete(where={"source": source})

    def query(self, query_embedding: list[float], top_k: int) -> list[VectorSearchHit]:
        """用查询向量搜索最接近的片段并转换为统一命中对象。

        距离换算为 1/(1+非负距离)，越近分数越高；缺少部分元数据时使用默认值。
        """
        result = self.collection.query(
            query_embeddings=[query_embedding],  # type: ignore[arg-type]
            n_results=top_k,
            include=["documents", "metadatas", "distances"],  # type: ignore[list-item]
        )
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        hits = []
        for index, document in enumerate(documents):
            metadata = metadatas[index] if index < len(metadatas) else {}
            distance = float(distances[index]) if index < len(distances) else 1.0
            hits.append(
                VectorSearchHit(
                    chunk_id=int(metadata["db_id"]) if metadata.get("db_id") is not None else None,
                    source=str(metadata.get("source", "")),
                    source_index=int(metadata.get("source_index", 0)),
                    content=document or "",
                    score=1.0 / (1.0 + max(0.0, distance)),
                )
            )
        return hits

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """检查嵌入能力后请求一批文本向量。

        不可用时抛出记录的原因；成功结果应与输入文本顺序对应。
        """
        if not self.can_embed:
            raise VectorStoreUnavailable(self.error or "Chroma + text-embedding-3-small 主检索方案不可用")
        return self._embed(texts)

    def count(self) -> int:
        """返回当前集合中的向量条目数量。

        未启用或缺少必要配置时返回零，而非尝试访问未创建的集合。
        """
        if not self.can_embed:
            return 0
        return int(self.collection.count())

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """向配置的嵌入接口批量请求文本向量。

        独立嵌入地址和密钥优先，未配置时复用对话服务配置；空白文本替换为一个空格。
        每批最多十条，结果按批内接口编号排序后拼接，核对数量和非空性。
        """
        base_url = self.settings.embedding_base_url or self.settings.openai_base_url
        api_key = self.settings.embedding_api_key or self.settings.openai_api_key
        headers = {"Authorization": f"Bearer {api_key}"}
        vectors = []
        for start in range(0, len(texts), _EMBEDDING_BATCH_SIZE):
            batch = texts[start:start + _EMBEDDING_BATCH_SIZE]
            payload = {
                "model": self.settings.openai_embedding_model,
                "input": [text if text.strip() else " " for text in batch],
            }
            response = httpx.post(
                f"{base_url}/embeddings",
                headers=headers,
                json=payload,
                timeout=self.settings.embedding_timeout_seconds,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                diagnostics.degraded(
                    "knowledge.embed", "embedding_http_status", exc,
                    status=response.status_code,
                )
                raise
            # 接口编号从每批的零重新开始，不能把所有批次混合排序。
            rows = sorted(response.json().get("data", []), key=lambda item: item.get("index", 0))
            embeddings = [row.get("embedding") for row in rows]
            if len(embeddings) != len(batch) or any(not embedding for embedding in embeddings):
                raise VectorStoreUnavailable("OpenAI embeddings 接口返回向量数量不匹配")
            vectors.extend([[float(value) for value in embedding] for embedding in embeddings])
        return vectors

    def _resolve_path(self, value: str) -> Path:
        """将索引目录配置解析成实际文件路径。

        绝对路径直接使用，相对路径以项目根目录为基准。
        """
        path = Path(value)
        return path if path.is_absolute() else self.settings.project_root / path

    def _id(self, chunk_id: int) -> str:
        """把数据库片段编号转换成向量库中的稳定文本编号。

        固定前缀便于跨两种存储对应同一片段。
        """
        return f"knowledge-chunk-{chunk_id}"


ChromaKnowledgeVectorStore = ChromaKnowledgeStore
