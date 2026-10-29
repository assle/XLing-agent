from __future__ import annotations

import json
from pathlib import Path

from app.core.config import Settings
from app.models.entities import KnowledgeChunk
from app.services.ai import AiClient
from app.services.knowledge import KnowledgeService, SearchResult
from evals.config import EvalSettings
from evals.rag.runner import _build_retrieve_fn, evaluate
from tests.support import DatabaseHarness


class FakeAi:
    def __init__(self, document: str = "假设性说明"):
        self.document = document

    def generate_hyde_document(self, query: str) -> str:
        return self.document

    def rerank(self, query: str, candidates: list[dict]) -> list[tuple[int, float]]:
        scores = [9.0, 8.8, 8.7, 2.0, 1.0]
        return [(index, scores[index]) for index in range(len(candidates))]


def make_service(db, ai_client=None) -> KnowledgeService:
    return KnowledgeService(
        db,
        Settings(_env_file=None, knowledge_vector_enabled=False),
        ai_client=ai_client,
    )


def test_hyde_uses_direct_and_hypothetical_rankings_with_rrf(monkeypatch):
    harness = DatabaseHarness()
    try:
        with harness.sessions() as db:
            rows = [KnowledgeChunk(source=f"source-{index}", source_index=0, content=f"正文 {index}")
                    for index in range(3)]
            db.add_all(rows)
            db.commit()
            service = make_service(db, FakeAi())
            queries = []

            def search(query: str, top_k: int):
                queries.append(query)
                ranked = [rows[0], rows[1]] if query == "短问题" else [rows[1], rows[2]]
                return [SearchResult(row.id, row.source, row.content, 1.0) for row in ranked]

            monkeypatch.setattr(service, "_retrieve_vector", search)
            found = service.retrieve_hyde("短问题", 2)

            assert queries == ["短问题", "短问题\n假设性说明"]
            assert [item.chunk_id for item in found] == [rows[1].id, rows[0].id]
    finally:
        harness.close()


def test_hyde_failure_keeps_direct_vector_results(monkeypatch):
    harness = DatabaseHarness()
    try:
        with harness.sessions() as db:
            row = KnowledgeChunk(source="sleep", source_index=0, content="固定起床时间")
            db.add(row)
            db.commit()
            service = make_service(db, FakeAi())
            monkeypatch.setattr(
                service.ai_client,
                "generate_hyde_document",
                lambda query: (_ for _ in ()).throw(RuntimeError("model unavailable")),
            )
            monkeypatch.setattr(
                service,
                "_retrieve_vector",
                lambda query, top_k: [SearchResult(row.id, row.source, row.content, 0.8)],
            )

            found = service.retrieve_hyde("睡眠", 1)

            assert [item.chunk_id for item in found] == [row.id]
    finally:
        harness.close()


def test_cliff_cutoff_respects_minimum_maximum_and_empty_input():
    ranked = [SearchResult(i, "source", str(i), score)
              for i, score in enumerate([0.95, 0.92, 0.88, 0.12, 0.08])]

    assert [item.score for item in KnowledgeService._cliff_cutoff(ranked, 5)] == [0.95, 0.92, 0.88]
    assert len(KnowledgeService._cliff_cutoff(ranked, 4, min_results=4)) == 4
    assert KnowledgeService._cliff_cutoff([], 4) == []
    assert KnowledgeService._cliff_cutoff(ranked, 0) == []


def test_hyde_eval_strategy_applies_rerank_before_cliff_cutoff():
    harness = DatabaseHarness()
    try:
        with harness.sessions() as db:
            service = make_service(db, FakeAi())
            candidates = [SearchResult(i, "source", f"正文 {i}", 0.0) for i in range(5)]
            service.retrieve_hyde = lambda query, top_k: candidates
            settings = EvalSettings(_env_file=None, rag_eval_rerank_candidate_pool=5)

            found = _build_retrieve_fn(service, "hyde-rerank", settings)("问题", 4)

            assert [item.score for item in found] == [9.0, 8.8, 8.7]
    finally:
        harness.close()


def test_hyde_eval_writes_independent_report_and_summary():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        output_dir = Path(tmp)
        report_path = output_dir / "report.json"
        summary_path = output_dir / "summary.json"
        settings = EvalSettings(_env_file=None).model_copy(update={
            "rag_eval_dataset": "evals/rag/xling-rag-eval.jsonl",
            "rag_eval_database_url": f"sqlite:///{output_dir / 'eval.db'}",
            "knowledge_vector_enabled": False,
            "ai_provider": "mock",
            "rag_eval_ai_provider": "mock",
            "rag_eval_output": str(report_path),
            "rag_eval_baseline_output": str(output_dir / "baseline.json"),
            "rag_eval_hyde_rerank_output": str(summary_path),
        })

        report = evaluate(settings, strategy="hyde-rerank")

        assert report["totalCases"] == 100
        assert "HyDE" in report["retrieval"]
        assert json.loads(summary_path.read_text())["retrieval"] == report["retrieval"]


def test_hyde_prompt_is_not_returned_as_user_answer(monkeypatch):
    client = AiClient(Settings(_env_file=None, ai_provider="mock"))
    messages_seen = []

    def complete(messages):
        messages_seen.extend(messages)
        return "  用于检索的说明  "

    monkeypatch.setattr(client, "complete", complete)

    assert client.generate_hyde_document("失眠怎么办") == "用于检索的说明"
    assert "不直接回答用户" in messages_seen[0].content
    assert messages_seen[-1].content == "失眠怎么办"
