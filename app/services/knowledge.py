from __future__ import annotations

import asyncio
import hashlib
import heapq
import json
import math
import re
from dataclasses import dataclass

import numpy as np
from pypdf import PdfReader
from sqlalchemy.orm import Session

from app.core import diagnostics
from app.core.config import Settings
from app.models.entities import KnowledgeChunk, KnowledgeSource
from app.services.bge_retrieval import BgeRetrieverUnavailable
from app.services.chunking import chunk_text, semantic_chunk_text
from app.services.lexical_index import lexical_index, tokenize
from app.services.vector_store import FALLBACK_RETRIEVAL_LABEL, PRIMARY_RETRIEVAL_LABEL, ChromaKnowledgeStore


@dataclass
class SearchResult:
    chunk_id: int | None
    source: str
    content: str
    score: float


class KnowledgeService:
    def __init__(self, db: Session, settings: Settings, ai_client=None, retriever=None):
        """准备知识数据库、默认向量库及可选评估依赖。

        ai_client 用于查询改写和重排，retriever 仅由离线评估显式注入；在线默认构造不会选择实验检索器。
        """
        self.db = db
        self.settings = settings
        self.vector_store = ChromaKnowledgeStore(settings)
        self.ai_client = ai_client
        # 实验检索器只由离线评估显式注入，在线默认构造不读取实验切换配置。
        self.retriever = retriever
        self.vector_fallback_used = False

    def count(self) -> int:
        """统计数据库中的全部知识片段数量。

        不检查向量库是否包含相同片段，索引一致性另行验证。
        """
        return self.db.query(KnowledgeChunk).count()

    def _source_fingerprint(self, content: str, semantic: bool) -> str:
        identity = ["semantic-sentence-v1" if semantic else "structural-sentence-v1", content,
                    self.settings.knowledge_chunk_size, self.settings.knowledge_chunk_overlap]
        if semantic:
            identity.extend([self.settings.openai_embedding_model,
                             self.settings.embedding_base_url or self.settings.openai_base_url])
        return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def _ingestion_fingerprint(source_fingerprint: str, chunks: list[str]) -> str:
        return hashlib.sha256(json.dumps([source_fingerprint, chunks], ensure_ascii=False).encode()).hexdigest()

    def _split_content(self, content: str) -> tuple[list[str], str]:
        size, overlap = self.settings.knowledge_chunk_size, self.settings.knowledge_chunk_overlap
        if self.vector_store.can_embed:
            try:
                chunks = semantic_chunk_text(content, size, overlap, self.vector_store.embed_texts)
                return chunks, self._source_fingerprint(content, True)
            except Exception as exc:
                self._handle_vector_error("chunk", exc)
        return chunk_text(content, size, overlap), self._source_fingerprint(content, False)

    def ensure_source(self, source: str, content: str) -> int:
        """Reuse unchanged segmentation; keep checking vector index integrity.

        The derived source fingerprint is saved in the same SQL transaction as its
        chunks. Old installations without fingerprints re-ingest built-in sources.
        """
        existing = [row.content for row in self.db.query(KnowledgeChunk)
                    .filter(KnowledgeChunk.source == source).order_by(KnowledgeChunk.source_index).all()]
        record = self.db.get(KnowledgeSource, source)
        expected = self._source_fingerprint(content, self.vector_store.can_embed)
        if record is not None and record.fingerprint == self._ingestion_fingerprint(expected, existing):
            if self.vector_store.can_embed:
                self._ensure_vector_index()
            return len(existing)
        chunks, fingerprint = self._split_content(content)
        if existing == chunks:
            self.db.merge(KnowledgeSource(source=source, fingerprint=self._ingestion_fingerprint(fingerprint, chunks)))
            if self.vector_store.can_embed:
                self._ensure_vector_index()
            self.db.commit()
            return len(existing)
        return self._replace_source(source, chunks, fingerprint)

    def status(self) -> dict:
        """汇总知识库规模、向量配置及组件错误信息。

        向量计数失败被记录在返回状态中；数据库计数错误仍会向上传递。
        """
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
            "vectorError": vector_error,
        }

    def rebuild_vector_index(self) -> int:
        """以数据库现有片段为依据同步向量索引并提交保存的向量。

        向量组件不可用时抛错；返回处理的数据库行数，具体同步失败行为由是否强制向量检索的配置决定。
        """
        if not self.vector_store.can_embed:
            raise RuntimeError(getattr(self.vector_store, "error", "") or "Chroma 向量库不可用")
        rows = self.db.query(KnowledgeChunk).order_by(KnowledgeChunk.source.asc(), KnowledgeChunk.source_index.asc()).all()
        self._sync_vector_chunks(rows)
        self.db.commit()
        return len(rows)

    def ingest(self, source: str, content: str) -> int:
        """Replace a source with semantic passages, or structural passages offline."""
        chunks, fingerprint = self._split_content(content)
        return self._replace_source(source, chunks, fingerprint)

    def _replace_source(self, source: str, chunks: list[str], fingerprint: str) -> int:
        # External vector writes do not share the SQL transaction.
        self._delete_vector_source(source)
        self.db.query(KnowledgeChunk).filter(KnowledgeChunk.source == source).delete()
        rows = [KnowledgeChunk(source=source, source_index=i, content=chunk) for i, chunk in enumerate(chunks)]
        self.db.add_all(rows)
        self.db.flush()
        self._index_vector_chunks(rows)
        self.db.merge(KnowledgeSource(source=source, fingerprint=self._ingestion_fingerprint(fingerprint, chunks)))
        self.db.commit()
        return len(rows)

    def ingest_file(self, filename: str, data: bytes) -> int:
        """从上传字节提取正文并按文件名导入知识库。

        PDF 文件使用文本提取，其余按 UTF-8 解码并忽略无法解码的字节；不对扫描图片进行文字识别。
        """
        lower = filename.lower()
        if lower.endswith(".pdf"):
            text = extract_pdf(data)
        else:
            text = data.decode("utf-8", errors="ignore")
        return self.ingest(filename, text)

    def retrieve(self, query: str, top_k: int | None = None) -> list[SearchResult]:
        """执行单次检索，可优先使用离线评估显式传入的检索器。

        top_k 为空或为零时使用配置值；实验检索器不可用时回到当前默认路径，其他异常仍会抛出。
        """
        with diagnostics.stage("knowledge.retrieve"):
            top_k = top_k or self.settings.knowledge_top_k
            if self.retriever is not None:
                try:
                    chunks = self.db.query(KnowledgeChunk).order_by(KnowledgeChunk.source.asc(), KnowledgeChunk.source_index.asc()).all()
                    ranked = self.retriever.retrieve(query, chunks, top_k)
                    return self._expand_best([
                        SearchResult(item.chunk.id, item.chunk.source, item.chunk.content, item.score)
                        for item in ranked
                    ], top_k)
                except BgeRetrieverUnavailable as exc:
                    diagnostics.degraded("knowledge.retrieve", "offline_bge_unavailable", exc)
            return self._retrieve_current(query, top_k)

    def _retrieve_current(self, query: str, top_k: int) -> list[SearchResult]:
        """Fuse complementary dense/BM25 scores over the union, then expand once."""
        pool = top_k * 3
        dense, query_vector = self._search_vector(query, pool)
        if not dense:
            return self._retrieve_hybrid(query, top_k)
        rows, index = self._lexical_snapshot()
        sparse_scores = index.scores(query)
        sparse_ids = heapq.nsmallest(pool, (i for i in sparse_scores if sparse_scores[i] > 0),
                                    key=lambda i: (-sparse_scores[i], i))
        candidate_ids = {row.chunk_id for row in dense} | {rows[i].id for i in sparse_ids}
        candidates = self.db.query(KnowledgeChunk).filter(KnowledgeChunk.id.in_(candidate_ids)).order_by(KnowledgeChunk.id).all()
        # Complement missing dense scores using the same query and persisted passage
        # embeddings, not zero scores for passages absent from the dense top-pool.
        try:
            vectors = np.asarray([parse_embedding(row.embedding_json) for row in candidates], dtype=np.float32)
            q = np.asarray(query_vector, dtype=np.float32)
            cosine = vectors @ q / np.maximum(np.linalg.norm(vectors, axis=1) * np.linalg.norm(q), 1e-12)
            by_id = {rows[i].id: score for i, score in sparse_scores.items()}
            lexical = np.asarray([by_id.get(row.id, 0.0) for row in candidates])
            scores = normalized_fusion(cosine, lexical)
        except (TypeError, ValueError) as exc:
            self._handle_vector_error("fusion", exc)
            return self._retrieve_hybrid(query, top_k)
        ranked = sorted([SearchResult(row.id, row.source, row.content, float(score))
                         for row, score in zip(candidates, scores, strict=True)],
                        key=lambda row: (-row.score, row.chunk_id))
        return self._expand_best(ranked[:top_k], top_k)

    async def aretrieve(
        self,
        query: str,
        top_k: int | None = None,
    ) -> list[SearchResult]:
        """把同步检索放到辅助线程中等待，减少阻塞异步对话入口。

        仍复用当前服务对象及依赖；这不是新的数据库会话，也不改变底层检索的同步实现。
        """
        return await asyncio.to_thread(self.retrieve, query, top_k)

    def retrieve_multi_query(self, query: str, top_k: int | None = None, base_retrieve=None) -> list[SearchResult]:
        """为一个问题生成多条查询，逐条检索后按片段编号合并。

        重复片段保留最高分，按分数排序后限制数量；没有模型客户端时直接使用单查询。
        """
        top_k = top_k or self.settings.knowledge_top_k
        base_retrieve = base_retrieve or self.retrieve
        if not self.ai_client:
            return base_retrieve(query, top_k)
        sub_queries = self.ai_client.generate_sub_queries(query, n=3)
        if not sub_queries:
            sub_queries = [query]
        seen: dict[int | None, SearchResult] = {}
        # 各查询依次执行，按片段编号去重；分数更高的重复命中替换旧结果。
        for sq in sub_queries:
            for result in base_retrieve(sq, top_k):
                key = result.chunk_id
                # 同编号只保留当前最高分；不同查询的分数直接比较，没有额外校准。
                if key not in seen or result.score > seen[key].score:
                    seen[key] = result
        # 按结果对象上的当前分数降序排列，保留更相关的候选。
        merged = sorted(seen.values(), key=lambda r: r.score, reverse=True)
        return self._expand_best(merged[:top_k], top_k)

    def retrieve_hyde(self, query: str, top_k: int | None = None) -> list[SearchResult]:
        """用原问题与 HyDE 两路向量结果做 RRF；失败时保留原问题检索。

        假设性文字只参与检索，不会进入回答上下文。该入口供离线对照使用，
        不改变当前在线默认的 dense + BM25 归一化融合。
        """
        top_k = top_k or self.settings.knowledge_top_k
        pool = top_k * 3
        direct = self._retrieve_vector(query, pool)
        if not direct:
            return self._retrieve_hybrid(query, top_k)
        rankings = [direct]
        if self.ai_client is not None:
            try:
                document = self.ai_client.generate_hyde_document(query)
                if document:
                    hypothetical = self._retrieve_vector(f"{query}\n{document}", pool)
                    if hypothetical:
                        rankings.append(hypothetical)
            except Exception as exc:
                diagnostics.degraded("knowledge.hyde", "hyde_unavailable", exc)
        fused = self._rrf_fuse(rankings, pool)
        return self._expand_best(fused, top_k)

    @staticmethod
    def _cliff_cutoff(
        ranked: list[SearchResult],
        top_k: int,
        *,
        min_results: int = 3,
        absolute_gap: float = 0.5,
        relative_gap: float = 0.25,
    ) -> list[SearchResult]:
        """在最低保留数之后寻找首个明显分数落差，并最多返回 top_k 项。"""
        upper = min(max(0, top_k), len(ranked))
        lower = min(max(1, min_results), upper)
        cutoff = upper
        for index in range(lower - 1, upper - 1):
            current = ranked[index].score
            following = ranked[index + 1].score
            gap = current - following
            if gap >= absolute_gap or gap / (abs(current) + 1e-6) >= relative_gap:
                cutoff = index + 1
                break
        return ranked[:cutoff]

    def retrieve_hybrid_rrf(self, query: str, top_k: int | None = None) -> list[SearchResult]:
        """分别取得向量或备用结果及关键词结果，再按名次融合。

        离线 RRF 对照每路候选数量为 top_k 的三倍；向量不可用时直接回退，不重复融合词法信号。
        融合使用排名倒数加权，避免直接比较不同评分方法的原始数值。
        """
        top_k = top_k or self.settings.knowledge_top_k
        pool = top_k * 3
        vector_results = self._retrieve_vector(query, pool)
        if not vector_results:
            return self._retrieve_hybrid(query, top_k)
        # 关键词通道同样参与排序融合，不因向量通道已有结果而跳过。
        bm25_results = self._retrieve_bm25(query, pool)
        fused = self._rrf_fuse([vector_results, bm25_results], top_k)
        return self._expand_best(fused, top_k)
    def _retrieve_bm25(self, query: str, top_k: int) -> list[SearchResult]:
        """Reuse content-keyed BM25 weights and visit only query term postings.

        Read current scalar columns so ingestion by another session/process changes
        the cache key immediately, without holding stale ORM objects or embeddings.
        """
        rows, index = self._lexical_snapshot()
        return [SearchResult(rows[i].id, rows[i].source, rows[i].content, score)
                for i, score in index.search(query, top_k)]

    def _lexical_snapshot(self):
        rows = self.db.query(KnowledgeChunk.id, KnowledgeChunk.source, KnowledgeChunk.content).order_by(
            KnowledgeChunk.source.asc(), KnowledgeChunk.source_index.asc()
        ).all()
        return rows, lexical_index(tuple(row.content for row in rows))

    @staticmethod
    def _rrf_fuse(result_lists: list[list[SearchResult]], top_k: int, k: int = 60) -> list[SearchResult]:
        """通过排名倒数融合多份检索结果。

        同一片段在各列表中的分数累计为 1/(k+名次)，名次从一开始；保留首次遇到的正文与来源。
        """
        scores: dict[int | None, float] = {}
        best: dict[int | None, SearchResult] = {}
        for result_list in result_lists:
            for rank, result in enumerate(result_list, start=1):
                key = result.chunk_id
                # 累加的是名次转换后的分数，排名越靠前贡献越大，不直接相加原始相似度。
                scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
                if key not in best:
                    best[key] = result
        # 按各片段累计的排名融合分数排序。
        fused = sorted(scores.keys(), key=lambda key: scores[key], reverse=True)
        return [
            SearchResult(best[key].chunk_id, best[key].source, best[key].content, scores[key])
            for key in fused[:top_k]
        ]

    def _retrieve_hybrid(self, query: str, top_k: int) -> list[SearchResult]:
        """对数据库全部片段计算本地词项和关键词混合分数。

        过滤零分结果，按降序取候选后为最佳片段补充相邻上下文。
        """
        ranked = [
            SearchResult(chunk.id, chunk.source, chunk.content, hybrid_score(query, chunk.content))
            for chunk in self.db.query(KnowledgeChunk).all()
        ]
        ranked = [item for item in ranked if item.score > 0]
        # 按本地混合分数排列结果，随后只保留请求数量。
        ranked.sort(key=lambda item: item.score, reverse=True)
        return self._expand_best(ranked[:top_k], top_k)

    def _retrieve_vector(self, query: str, top_k: int) -> list[SearchResult]:
        """Dense-only control retained for reproducible offline comparisons."""
        return self._search_vector(query, top_k)[0]

    def _search_vector(self, query: str, top_k: int) -> tuple[list[SearchResult], list[float]]:
        """确保索引可用后将查询转换为向量并查找相近片段。

        匹配编号存在于数据库时优先使用数据库当前正文；向量操作错误按强制配置抛出或返回空列表。
        """
        with diagnostics.stage("knowledge.vector.retrieve"):
            if not self.vector_store.can_embed:
                return [], []
            try:
                self._ensure_vector_index()
                query_embedding = self.vector_store.embed_texts([query])[0]
                hits = self.vector_store.query(query_embedding, top_k)
            except Exception as exc:
                self._handle_vector_error("retrieve", exc)
                return [], []
            ids = [hit.chunk_id for hit in hits if hit.chunk_id is not None]
            current = {row.id: row for row in self.db.query(KnowledgeChunk).filter(KnowledgeChunk.id.in_(ids)).all()}
            results = []
            for hit in hits:
                chunk = current.get(hit.chunk_id) if hit.chunk_id is not None else None
                results.append(
                    SearchResult(
                        chunk.id if chunk is not None else hit.chunk_id,
                        chunk.source if chunk is not None else hit.source,
                        chunk.content if chunk is not None else hit.content,
                        hit.score,
                    )
                )
            return results, query_embedding

    def _ensure_vector_index(self) -> None:
        """检查索引数量、数据库向量及片段编号集合是否完整。

        任一条件不符则同步索引并提交；不能仅凭片段数量一致判断索引可复用。
        """
        rows = self.db.query(KnowledgeChunk).order_by(KnowledgeChunk.source.asc(), KnowledgeChunk.source_index.asc()).all()
        if not rows:
            return
        if (
            self.vector_store.count() == len(rows)
            and all(row.embedding_json for row in rows)
            # 即使数量相同，也必须核对具体编号，防止旧片段替换新片段而数量恰好不变。
            and self.vector_store.has_exact_chunk_ids(rows)
        ):
            return
        self._sync_vector_chunks(rows)
        self.db.commit()

    def _delete_vector_source(self, source: str) -> None:
        """删除向量库中指定来源的片段。

        组件不可用时直接返回；删除失败交由统一向量错误策略决定是否抛出。
        """
        if not self.vector_store.can_embed:
            return
        try:
            self.vector_store.delete_source(source)
        except Exception as exc:
            self._handle_vector_error("delete_source", exc)

    def _index_vector_chunks(self, chunks: list[KnowledgeChunk]) -> None:
        """为新片段补齐向量，并写入向量库。

        向量文本同时放回数据库对象，但本函数不提交数据库；错误按配置允许回退或继续抛出。
        """
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
        """将指定片段及其向量同步为索引的有效集合。

        更新数据库对象上的向量文本，再调用索引同步去除过时编号；提交由外层负责。
        """
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
        """复用可解析的已有向量，仅请求缺失片段的向量。

        用原列表位置把新向量放回对应片段，最后检查结果数量，避免正文与向量错位。
        """
        embeddings: list[list[float] | None] = []
        missing_indexes = []
        missing_texts = []
        for index, chunk in enumerate(chunks):
            embedding = parse_embedding(chunk.embedding_json)
            embeddings.append(embedding)
            if embedding is None:
                missing_indexes.append(index)
                missing_texts.append(chunk.content)
        # 只请求缺少向量的正文，再按记录下的位置回填，减少重复外部调用。
        if missing_texts:
            new_embeddings = self.vector_store.embed_texts(missing_texts)
            for index, embedding in zip(missing_indexes, new_embeddings):
                embeddings[index] = embedding
        resolved = [embedding for embedding in embeddings if embedding is not None]
        # 数量不一致意味着部分片段没有对应向量，阻止错位数据进入索引。
        if len(resolved) != len(chunks):
            raise ValueError("Embedding response count did not match knowledge chunks.")
        return resolved

    def _handle_vector_error(self, action: str, exc: Exception) -> None:
        """统一处理向量操作失败。

        配置要求必须使用向量时重新抛错；允许备用方案时记录回退标志和日志，供本地检索继续。
        """
        if self.settings.knowledge_vector_required:
            raise exc
        self.vector_fallback_used = True
        diagnostics.degraded(f"knowledge.vector.{action}", "vector_unavailable", exc)

    def _expand_best(self, ranked: list[SearchResult], top_k: int) -> list[SearchResult]:
        """只为排名第一的片段补充相邻内容，再附上其余结果。

        保持最佳片段编号和分数，避免重复编号；最多 top_k 项、3072 字。
        """
        if not ranked:
            return []
        results = []
        remaining = 3072
        seen = set()
        for rank, item in enumerate(ranked):
            if item.chunk_id in seen:
                continue
            expanded = self._expand(item) if rank == 0 else item
            content = expanded.content if len(expanded.content) <= remaining else item.content[:remaining]
            if not content:
                break
            results.append(SearchResult(item.chunk_id, item.source, content, item.score))
            remaining -= len(content)
            seen.add(item.chunk_id)
            if len(results) == top_k:
                break
        return results

    def _expand(self, result: SearchResult) -> SearchResult:
        """为存在数据库编号的检索片段补上同来源的前后邻片段。

        按来源内顺序拼接正文，保留中心片段的编号和分数；编号缺失时返回原结果。
        """
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


def normalized_fusion(dense: np.ndarray, sparse: np.ndarray, alpha: float = .98) -> np.ndarray:
    """Min-max convex combination on fully scored candidates (Bruch et al.)."""
    def normalize(values: np.ndarray) -> np.ndarray:
        if not np.isfinite(values).all():
            raise ValueError("Retrieval scores must be finite")
        low, high = float(values.min()), float(values.max())
        return (values - low) / (high - low) if high > low else np.zeros_like(values)
    return alpha * normalize(dense) + (1 - alpha) * normalize(sparse)


def hybrid_score(query: str, content: str) -> float:
    """组合词频相似度与关键词命中比例得到本地评分。

    两者分别占四分之三和四分之一；此分数用于备用排序，不是模型置信度。
    """
    return token_cosine(query, content) * 0.75 + keyword_score(query, content) * 0.25


def parse_embedding(raw: str | None) -> list[float] | None:
    """将数据库保存的向量文本解析成非空数字列表。

    空值、格式错误或非数字元素返回 None，让调用方重新生成向量；不验证向量维度是否匹配模型。
    """
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


def token_cosine(left: str, right: str) -> float:
    """根据两段文本的词频向量计算方向相似度。

    对相同词项的频次相乘求和，再除以两个向量的长度；空文本或零长度向量返回零。
    """
    left_counts = counts(tokenize(left))
    right_counts = counts(tokenize(right))
    if not left_counts or not right_counts:
        return 0.0
    dot = sum(value * right_counts.get(key, 0) for key, value in left_counts.items())
    left_norm = math.sqrt(sum(value * value for value in left_counts.values()))
    right_norm = math.sqrt(sum(value * value for value in right_counts.values()))
    return 0.0 if left_norm == 0 or right_norm == 0 else dot / (left_norm * right_norm)


def keyword_score(query: str, content: str) -> float:
    """计算查询词项在正文中出现的比例。

    按标点和空白切分，仅保留长度至少为二的词项；按子串命中，重复词项也参与计数。
    """
    terms = [term for term in re.split(r"[\s，。！？、；：,.!?;:]+", query.lower()) if len(term) >= 2]
    if not terms:
        return 0.0
    lower = content.lower()
    matched = sum(1 for term in terms if term in lower)
    return min(1.0, matched / len(terms))


def counts(values: list[str]) -> dict[str, int]:
    """统计列表中每个词项出现的次数。

    返回新字典，供词频相似度计算使用，不修改传入列表。
    """
    result: dict[str, int] = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return result


def extract_pdf(data: bytes) -> str:
    """从 PDF 字节中逐页提取可读取文本并用换行连接。

    没有可提取文本的页面按空文本处理；扫描图片不会自动识别，文件解析错误直接抛出。
    """
    from io import BytesIO

    reader = PdfReader(BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)
