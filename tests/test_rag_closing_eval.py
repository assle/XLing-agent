import asyncio
import json

import pytest

from app.core.config import Settings
from app.services.knowledge import SearchResult
from evals.rag import closing
from evals.rag.closing import answer_messages, require_review, score_case, summarize


def case(answerable=True):
    return {"id": "test-evidence", "answerable": answerable, "safetyCritical": True,
            "expectedEvidence": [{"source": "risk-policy.md", "requiredSpans": ["立即联系现实支持"]}] if answerable else []}


def test_source_name_and_shared_keywords_do_not_replace_required_evidence():
    irrelevant = SearchResult(1, "risk-policy.md", "LOW 无明确危险，可提供日常支持。", .9)
    unrelated_source = SearchResult(2, "other.md", "立即联系现实支持", .8)
    relevant = SearchResult(3, "risk-policy.md", "出现立即危险，应立即联系现实支持。", .7)
    row = score_case(case(), [irrelevant, unrelated_source, relevant], 4)
    assert row["firstRelevantRank"] == 3
    assert row["reciprocalRank"] == 1 / 3
    assert [item["relevant"] for item in row["retrieved"]] == [False, False, True]


def test_evidence_beyond_top_k_does_not_count_as_hit():
    noise = SearchResult(1, "risk-policy.md", "LOW 日常支持", .9)
    relevant = SearchResult(2, "risk-policy.md", "立即联系现实支持", .8)
    assert score_case(case(), [noise, relevant], 1)["hit"] is False


def test_unanswerable_queries_have_separate_denominator_and_empty_critical_subset_is_unknown():
    known = score_case(case(), [SearchResult(1, "risk-policy.md", "立即联系现实支持", .9)], 4)
    unknown = score_case(case(False), [], 4)
    for row in [known, unknown]:
        row.update(latencyMs=10, errorType=None)
    summary = summarize([known, unknown])
    assert summary["answerableCases"] == 1
    assert summary["hitAt4"] == 1
    assert summary["unanswerableCases"] == 1
    unknown_only = summarize([unknown])
    assert unknown_only["hitAt4"] is None
    assert unknown_only["safetyEvidenceMissRate"] is None


def test_human_review_is_bound_to_case_coverage_and_frozen_source_versions():
    cases = [case()]
    review = {"reviewStatus": "approved", "reviewerType": "human", "reviewedCaseIDs": ["test-evidence"],
              "datasetSHA256": "data-v1", "knowledgeSourceSHA256": {"risk-policy.md": "source-v1"}}
    require_review(review, cases, "data-v1", {"risk-policy.md": "source-v1"})
    with pytest.raises(ValueError, match="changed after review"):
        require_review(review, cases, "data-v1", {"risk-policy.md": "source-v2"})
    with pytest.raises(ValueError, match="every case"):
        require_review({**review, "reviewedCaseIDs": []}, cases, "data-v1", {"risk-policy.md": "source-v1"})
    with pytest.raises(ValueError, match="Human evidence review"):
        require_review({**review, "reviewerType": "engineering-agent"}, cases, "data-v1", {"risk-policy.md": "source-v1"})


def test_paired_answer_prompts_change_only_the_knowledge_context():
    without = answer_messages("我需要小步整理工作压力。", "")
    with_rag = answer_messages("我需要小步整理工作压力。", "今天、本周、接受不完美的任务")
    assert without[1:] == with_rag[1:]
    assert with_rag[0].content.replace("今天、本周、接受不完美的任务", "") == without[0].content


def test_configured_deployment_resources_are_identical_in_both_arms():
    resources = "测试机构：TEST；预约快照：TEST-SLOT-001（非真实服务）"
    without = answer_messages("本周怎样预约？", "", resources)
    with_rag = answer_messages("本周怎样预约？", "可先整理希望获得的支持", resources)
    assert resources in without[0].content and resources in with_rag[0].content
    assert with_rag[0].content.replace("可先整理希望获得的支持", "") == without[0].content


@pytest.mark.parametrize("provider", ["mock", "opneai", "openai "])
def test_unknown_or_simulated_provider_cannot_enter_real_answer_evaluation(tmp_path, provider):
    with pytest.raises(ValueError, match="supported real provider"):
        asyncio.run(closing.run_answers(Settings(_env_file=None, ai_provider=provider), tmp_path / "unused.json", tmp_path / "out.json"))
    assert not (tmp_path / "out.json").exists()


def retrieval_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(closing, "knowledge_fingerprints", lambda: {"source.md": "v1"})
    payload = {"status": "completed", "comparisonEligible": True,
               "knowledgeSourceSHA256": {"source.md": "v1"},
               "results": {"vector": [{"id": "pair-test", "retrieved": []}]},
               "cases": [{"id": "pair-test", "pairedAnswer": True, "question": "资料没有的值是多少？",
                          "answerable": False, "expectedEvidence": [], "insufficientEvidenceReason": "资料未记载"}]}
    path = tmp_path / "retrieval.json"
    path.write_text(json.dumps(payload))
    return path


def test_cancelled_second_arm_keeps_first_reply_and_attempt_count(tmp_path, monkeypatch):
    retrieval = retrieval_fixture(tmp_path, monkeypatch)

    class Client:
        calls = 0

        def __init__(self, settings):
            pass

        async def acomplete(self, messages):
            self.calls += 1
            if self.calls == 2:
                raise asyncio.CancelledError()
            self.last_completion_metadata = {"finishReason": "stop", "modelReturned": "test-alias"}
            return "first completed engineering test observation"

    monkeypatch.setattr(closing, "AiClient", Client)
    output = tmp_path / "answers.json"
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(closing.run_answers(Settings(_env_file=None, ai_provider="openai"), retrieval, output))
    result = json.loads(output.read_text())
    assert result["status"] == "interrupted"
    assert result["attempts"] == 2
    arms = result["pairs"][0]["arms"]
    assert sorted(item["status"] for item in arms.values()) == ["completed", "started"]
    assert any(item.get("reply") == "first completed engineering test observation" for item in arms.values())
    with pytest.raises(FileExistsError):
        asyncio.run(closing.run_answers(Settings(_env_file=None, ai_provider="openai"), retrieval, output))


def test_local_answer_model_identity_and_neutral_review_material(tmp_path, monkeypatch):
    retrieval = retrieval_fixture(tmp_path, monkeypatch)

    class Client:
        def __init__(self, settings):
            assert settings.ollama_model == "local-test-tag"

        async def acomplete(self, messages):
            self.last_completion_metadata = {"finishReason": "stop", "modelReturned": "local-test-tag"}
            return "资料不足，无法给出具体值。"

    monkeypatch.setattr(closing, "AiClient", Client)
    output = tmp_path / "answers.json"
    asyncio.run(closing.run_answers(Settings(_env_file=None, ai_provider="ollama", ollama_model="local-test-tag",
                                            openai_model="unused-remote-alias"), retrieval, output))
    result = json.loads(output.read_text())
    assert result["modelRequested"] == "local-test-tag"
    assert result["attempts"] == 2
    review = output.with_suffix(".review.html").read_text()
    assert "withoutRag" not in review and "withRag" not in review
    assert "<h3>A</h3>" in review and "<h3>B</h3>" in review
    assert "decisionRule" in review


@pytest.mark.parametrize("metadata", [None, {"modelReturned": "test-alias", "finishReason": "length"}])
def test_truncated_or_empty_generations_cannot_enter_answer_comparison(tmp_path, monkeypatch, metadata):
    retrieval = retrieval_fixture(tmp_path, monkeypatch)

    class Client:
        def __init__(self, settings):
            self.calls = 0

        async def acomplete(self, messages):
            self.calls += 1
            self.last_completion_metadata = metadata
            return "partial reply" if self.calls == 1 else ""

    monkeypatch.setattr(closing, "AiClient", Client)
    output = tmp_path / "answers.json"
    with pytest.raises(RuntimeError, match="Complete nonempty generations"):
        asyncio.run(closing.run_answers(Settings(_env_file=None, ai_provider="openai"), retrieval, output))
    result = json.loads(output.read_text())
    assert result["status"] == "invalid-generation" and result["comparisonEligible"] is False
    assert {a["status"] for a in result["pairs"][0]["arms"].values()} == {"empty-response", "incomplete-response"}
    assert not output.with_suffix(".review.html").exists()


def answer_review_fixture():
    pairs = [{"id": "one", "reviewMapping": {"A": "withRag", "B": "withoutRag"},
              "arms": {"withRag": {"status": "completed"}, "withoutRag": {"status": "completed"}}},
             {"id": "two", "reviewMapping": {"A": "withoutRag", "B": "withRag"},
              "arms": {"withRag": {"status": "completed"}, "withoutRag": {"status": "completed"}}}]
    answers = {"status": "completed", "comparisonEligible": True, "pairs": pairs, "rubricSHA256": "rubric-v1", "attempts": 4, "scope": "test"}
    review = {"reviewStatus": "approved", "reviewerType": "human", "answersSHA256": "answers-v1",
              "rubricSHA256": "rubric-v1", "judgements": [
                  {"id": "one", "winner": "A", "reason": "Has the required fact"},
                  {"id": "two", "winner": "A", "reason": "More useful"}]}
    return answers, review


def test_answer_review_decodes_neutral_mapping_and_counts_actual_successes():
    answers, review = answer_review_fixture()
    summary = closing.summarize_answer_review(answers, review, "answers-v1")
    assert summary["counts"] == {"withRag": 1, "withoutRag": 1, "tie": 0, "both-failed": 0}
    assert summary["successfulReplies"] == 4
    assert summary["reviewedPairs"] == 2


@pytest.mark.parametrize("failure", ["ineligible", "failed-arm"])
def test_answer_review_requires_complete_comparable_generations(failure):
    answers, review = answer_review_fixture()
    if failure == "ineligible":
        answers["comparisonEligible"] = False
    else:
        answers["pairs"][0]["arms"]["withRag"]["status"] = "failed"
    with pytest.raises(ValueError, match="eligible for comparison"):
        closing.summarize_answer_review(answers, review, "answers-v1")


@pytest.mark.parametrize("change", [
    {"reviewerType": "agent"}, {"answersSHA256": "answers-v2"}, {"rubricSHA256": "rubric-v2"},
    {"judgements": [{"id": "one", "winner": "tie", "reason": "same"}]},
    {"judgements": [{"id": "one", "winner": "A", "reason": "x"}, {"id": "one", "winner": "B", "reason": "y"}]},
    {"judgements": [{"id": "one", "winner": "A", "reason": None}, {"id": "two", "winner": "tie", "reason": "same"}]},
])
def test_answer_review_rejects_unreviewed_changed_incomplete_or_invalid_data(change):
    answers, review = answer_review_fixture()
    with pytest.raises(ValueError):
        closing.summarize_answer_review(answers, {**review, **change}, "answers-v1")
