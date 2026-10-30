from __future__ import annotations

import numpy as np
import pytest
from rank_bm25 import BM25Okapi

from app.core.config import Settings
from app.models.entities import KnowledgeChunk
from app.services.chunking import chunk_text, semantic_chunk_text
from app.services.knowledge import KnowledgeService, SearchResult, normalized_fusion
from app.services.lexical_index import LexicalIndex, lexical_index, tokenize
from tests.support import DatabaseHarness


def test_chunks_keep_heading_paths_and_do_not_cross_sections():
    chunks = chunk_text("# 支持\n## 睡眠\n固定起床时间。减少睡前刺激。\n## 压力\n把任务拆小。允许休息。", 64, 10)
    assert chunks == ["# 支持\n## 睡眠\n固定起床时间。\n减少睡前刺激。", "# 支持\n## 压力\n把任务拆小。\n允许休息。"]


def test_chunks_preserve_evidence_and_fit_budget_even_with_long_sentences():
    text = "# 标题\n" + "\n".join(f"第{i}项：" + "证据" * 20 + "。" for i in range(10))
    chunks = chunk_text(text, 100, 20)
    assert all(len(chunk) <= 100 for chunk in chunks)
    for sentence in text.splitlines()[1:]:
        assert any(sentence in chunk for chunk in chunks)
    long = "字" * 333
    assert "".join(chunk_text(long, 100, 0)) == long
    assert chunk_text(" \n ", 100, 10) == []


@pytest.mark.parametrize("size,overlap", [(0, 0), (10, 10), (10, -1)])
def test_chunks_reject_nonprogressing_parameters(size, overlap):
    with pytest.raises(ValueError):
        chunk_text("正文", size, overlap)


def test_postings_scores_match_bm25_reference_including_repeated_terms():
    corpus = ("焦虑时缓慢呼吸，焦虑时稳定。", "固定睡眠作息。", "焦虑与睡眠有关。", "家庭沟通边界。", "工作任务安排。")
    reference = BM25Okapi([tokenize(text) for text in corpus])
    index = LexicalIndex(corpus)
    for query in ("焦虑 焦虑 呼吸", "睡眠作息", "无匹配xyz", "家庭沟通", ""):
        expected = reference.get_scores(tokenize(query))
        actual = dict(index.search(query, 20))
        assert set(actual) == {i for i, score in enumerate(expected) if score > 0}
        for i, score in actual.items():
            assert score == pytest.approx(expected[i], abs=1e-12)
        assert [i for i, _ in index.search(query, 2)] == [
            int(i) for i in np.argsort(-expected, kind="stable") if expected[i] > 0
        ][:2]


def test_empty_lexical_corpus_and_content_keyed_cache():
    assert LexicalIndex(()).search("问题", 4) == []
    assert LexicalIndex(("---",)).search("问题", 4) == []
    assert lexical_index(("正文",)) is lexical_index(("正文",))
    assert lexical_index(("正文",)) is not lexical_index(("更新",))


def test_lexical_cache_observes_another_sessions_ingest():
    harness = DatabaseHarness()
    settings = Settings(_env_file=None, knowledge_vector_enabled=False)
    try:
        with harness.sessions() as reader, harness.sessions() as writer:
            service = KnowledgeService(reader, settings)
            ingest = KnowledgeService(writer, settings)
            for i in range(5):
                ingest.ingest(f"source-{i}", ["呼吸练习", "睡眠作息", "家庭关系", "工作任务", "运动休息"][i])
            assert service._retrieve_bm25("呼吸", 4)[0].content == "呼吸练习"
            reader.commit()
            ingest.ingest("source-0", "社交活动")
            assert service._retrieve_bm25("呼吸", 4) == []
            assert service._retrieve_bm25("社交", 4)[0].content == "社交活动"
    finally:
        harness.close()


def test_semantic_breakpoints_use_real_boundaries_not_sentence_vector_averages():
    units = ["甲" * 70 + "。", "乙" * 70 + "。", "丙" * 70 + "。", "丁" * 70 + "。"]
    calls = []
    def encode(sentences):
        calls.append(sentences)
        return [[1., 0.], [1., 0.], [0., 1.], [0., 1.]]
    chunks = semantic_chunk_text("# 标题\n" + "".join(units), 512, 64, encode)
    assert calls == [units]
    assert chunks == ["# 标题\n" + "\n".join(units[:2]), "# 标题\n" + "\n".join(units[2:])]


def test_constant_fusion_channel_adds_no_artificial_preference():
    assert normalized_fusion(np.array([1., 1.]), np.array([0., 2.])).tolist() == pytest.approx([0, .02])
    assert normalized_fusion(np.array([0., 2.]), np.array([1., 1.])).tolist() == pytest.approx([0, .98])


def test_sparse_only_candidate_gets_its_real_dense_score(monkeypatch):
    harness = DatabaseHarness()
    try:
        with harness.sessions() as db:
            rows = [KnowledgeChunk(source=str(i), source_index=0, content=text, embedding_json=vector)
                    for i, (text, vector) in enumerate([
                        ("睡眠", "[0, 1]"), ("呼吸", "[1, 0]"), ("家庭", "[-1, 0]"),
                        ("工作", "[-1, 0]"), ("运动", "[-1, 0]"),
                    ])]
            db.add_all(rows)
            db.commit()
            service = KnowledgeService(db, Settings(
                _env_file=None, knowledge_vector_enabled=False,
                knowledge_retrieval_strategy="normalized_fusion",
            ))
            monkeypatch.setattr(service, "_search_vector", lambda query, pool: (
                [SearchResult(rows[0].id, rows[0].source, rows[0].content, .5)], [1., 0.]))
            found = service.retrieve("呼吸", 1)
            assert found[0].chunk_id == rows[1].id
            assert found[0].score == pytest.approx(1.)
    finally:
        harness.close()


def test_source_fingerprint_avoids_reencoding_but_repairs_missing_sql_passages(monkeypatch):
    harness = DatabaseHarness()
    settings = Settings(_env_file=None, knowledge_vector_enabled=False)
    try:
        with harness.sessions() as db:
            service = KnowledgeService(db, settings)
            content = "# 主题\n## 睡眠\n固定起床。\n## 工作\n任务拆分。"
            assert service.ensure_source("source", content) == 2
            original = service._split_content
            def forbidden(_):
                raise AssertionError("Unchanged text must not be segmented again")
            monkeypatch.setattr(service, "_split_content", forbidden)
            assert service.ensure_source("source", content) == 2
            monkeypatch.setattr(service, "_split_content", original)
            db.query(KnowledgeChunk).filter(KnowledgeChunk.source_index == 1).delete()
            db.commit()
            assert service.ensure_source("source", content) == 2
            assert db.query(KnowledgeChunk).count() == 2
    finally:
        harness.close()


def test_semantic_failure_uses_structural_ingestion_only_when_fallback_allowed():
    harness = DatabaseHarness()
    try:
        with harness.sessions() as db:
            service = KnowledgeService(db, Settings(_env_file=None, knowledge_vector_enabled=False))
            class UnavailableEncoder:
                can_embed = True
                def embed_texts(self, texts):
                    raise RuntimeError("encoder unavailable")
            service.vector_store = UnavailableEncoder()
            chunks, _ = service._split_content("# 支持\n稳定作息。")
            assert chunks == ["# 支持\n稳定作息。"]
            assert service.vector_fallback_used
            service.settings.knowledge_vector_required = True
            with pytest.raises(RuntimeError, match="encoder unavailable"):
                service._split_content("# 支持\n稳定作息。")
    finally:
        harness.close()


def test_expansion_respects_total_budget_and_deduplicates_ids():
    harness = DatabaseHarness()
    try:
        with harness.sessions() as db:
            service = KnowledgeService(db, Settings(_env_file=None, knowledge_vector_enabled=False))
            rows = [SearchResult(i, "source", "证据" * 600, 1.) for i in range(4)]
            found = service._expand_best([rows[0], rows[0], *rows[1:]], 4)
            assert sum(len(row.content) for row in found) == 3072
            assert len({row.chunk_id for row in found}) == len(found)
    finally:
        harness.close()
