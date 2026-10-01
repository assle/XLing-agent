from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass

from pypdf import PdfReader
from sqlalchemy.orm import Session

from app.core import diagnostics
from app.core.config import Settings
from app.models.entities import KnowledgeChunk
from app.services.bge_retrieval import BgeRetrieverUnavailable
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
        self._bm25 = None

    def count(self) -> int:
        """统计数据库中的全部知识片段数量。

        不检查向量库是否包含相同片段，索引一致性另行验证。
        """
        return self.db.query(KnowledgeChunk).count()

    def ensure_source(self, source: str, content: str) -> int:
        """检查某来源的当前切片是否与数据库一致，必要时重新导入。

        内容和顺序相同时复用记录，并在向量可用时检查索引；否则完整替换该来源。
        """
        chunks = chunk_text(content, self.settings.knowledge_chunk_size, self.settings.knowledge_chunk_overlap)
        existing = [
            chunk.content
            for chunk in self.db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.source == source)
            .order_by(KnowledgeChunk.source_index.asc())
            .all()
        ]
        # 同时比较片段内容和顺序；仅来源名称相同不足以判断正文未变。
        if existing == chunks:
            if self.vector_store.can_embed:
                self._ensure_vector_index()
            return len(existing)
        return self.ingest(source, content)

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
        """按来源替换知识正文及片段，并尝试建立对应向量索引。

        先移除旧来源索引和数据库片段，再生成新片段、取得编号并提交；外部向量库与数据库不共享事务。
        """
        chunks = chunk_text(content, self.settings.knowledge_chunk_size, self.settings.knowledge_chunk_overlap)
        # 外部索引先删除旧来源；它与下面的数据库替换没有共同提交边界。
        self._delete_vector_source(source)
        self.db.query(KnowledgeChunk).filter(KnowledgeChunk.source == source).delete()
        rows = []
        for index, chunk in enumerate(chunks):
            row = KnowledgeChunk(source=source, source_index=index, content=chunk)
            self.db.add(row)
            rows.append(row)
        self.db.flush()
        self._index_vector_chunks(rows)
        self.db.commit()
        return len(chunks)

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
        """先尝试向量检索，有结果时扩展最佳片段，否则采用本地混合评分。

        向量检索正常但结果为空也会进入本地路径，不仅限于连接失败。
        """
        # 先走向量路径；结果为空或可回退的错误才让后面的本地评分接手。
        vector_results = self._retrieve_vector(query, top_k)
        if vector_results:
            return self._expand_best(vector_results, top_k)
        return self._retrieve_hybrid(query, top_k)

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

    def retrieve_hybrid_rrf(self, query: str, top_k: int | None = None) -> list[SearchResult]:
        """分别取得向量或备用结果及关键词结果，再按名次融合。

        两路检索在代码中顺序执行；每路候选数量为 top_k 的三倍。
        融合使用排名倒数加权，避免直接比较不同评分方法的原始数值。
        """
        top_k = top_k or self.settings.knowledge_top_k
        pool = top_k * 3
        vector_results = self._retrieve_vector(query, pool)
        if not vector_results:
            vector_results = self._retrieve_hybrid(query, pool)
        # 关键词通道同样参与排序融合，不因向量通道已有结果而跳过。
        bm25_results = self._retrieve_bm25(query, pool)
        fused = self._rrf_fuse([vector_results, bm25_results], top_k)
        return self._expand_best(fused, top_k)

    def retrieve_with_rerank(self, query: str, top_k: int | None = None, candidate_pool: int = 20) -> list[SearchResult]:
        """先取较大候选集合，再用模型相关性评分重新排序。

        candidate_pool 控制候选数量；无模型时直接截取原顺序，缺少某候选分数时按零分处理。
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
        # 按结果对象上的当前分数降序排列，保留更相关的候选。
        reranked.sort(key=lambda r: r.score, reverse=True)
        return reranked[:top_k]


    def _retrieve_bm25(self, query: str, top_k: int) -> list[SearchResult]:
        """使用 BM25（依据词频和文档长度排序的方法）检索数据库片段。

        每次按当前片段构建词项集合，缺少依赖或无片段时返回空列表，仅保留正分结果。
        """
        chunks = self.db.query(KnowledgeChunk).order_by(
            KnowledgeChunk.source.asc(), KnowledgeChunk.source_index.asc()
        ).all()
        if not chunks:
            return []
        corpus = [tokenize(chunk.content) for chunk in chunks]
        try:
            from rank_bm25 import BM25Okapi
        except ImportError as exc:
            diagnostics.degraded("knowledge.bm25", "bm25_unavailable", exc)
            return []
        bm25 = BM25Okapi(corpus)
        scores = bm25.get_scores(tokenize(query))
        # 使用评分数组中的分数排序，同时保留原片段位置。
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
        """确保索引可用后将查询转换为向量并查找相近片段。

        匹配编号存在于数据库时优先使用数据库当前正文；向量操作错误按强制配置抛出或返回空列表。
        """
        with diagnostics.stage("knowledge.vector.retrieve"):
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

        保持最佳片段编号和分数，避免再次加入同编号片段；结果最多取 top_k 项。
        """
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


def chunk_text(content: str, size: int, overlap: int) -> list[str]:
    """压缩正文空白后按固定长度和重叠量切片。

    size 为片段长度，overlap 为重叠字符数；步长至少为一，避免重叠过大导致循环不前进。
    """
    text = re.sub(r"\s+", " ", content or "").strip()
    if not text:
        return []
    chunks = []
    start = 0
    # 重叠减少每次前进距离；至少前进一个字符，保证循环能结束。
    step = max(1, size - overlap)
    while start < len(text):
        chunks.append(text[start:start + size])
        start += step
    return chunks


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


def tokenize(text: str) -> list[str]:
    """把文本拆成英文数字词、中文单字和中文相邻双字组合。

    中文双字组合基于移除非中文字符后的文本，提供不依赖额外分词库的粗略匹配特征。
    """
    words = re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]", text.lower())
    grams = words[:]
    compact = "".join(ch for ch in text.lower() if "\u4e00" <= ch <= "\u9fff")
    grams.extend(compact[i:i + 2] for i in range(max(0, len(compact) - 1)))
    return [item for item in grams if item.strip()]


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


def _matches_stage(chunk_stage: str | None, target_stage: str) -> bool:
    """判断片段的阶段标记是否覆盖目标阶段。

    未设置、空值或 all 表示通用；其余按逗号分隔并去掉首尾空白后进行精确匹配。
    """
    if chunk_stage is None or chunk_stage == "" or chunk_stage == "all":
        return True
    stages = [s.strip() for s in chunk_stage.split(",")]
    return target_stage in stages
