from __future__ import annotations

import hashlib

from app.core.config import Settings
from app.core.versioning import ArtifactVersionResolver


def test_version_manifest_is_stable_and_covers_reproducibility_inputs(tmp_path):
    knowledge = tmp_path / "knowledge.md"
    knowledge.write_text("备考焦虑支持知识", encoding="utf-8")
    dataset = tmp_path / "eval.jsonl"
    dataset.write_text('{"id":"case-1"}\n', encoding="utf-8")
    settings = Settings(
        ai_provider="mock",
        ollama_model="chat-v1",
        ollama_classifier_model="classifier-v1",
        openai_embedding_model="embedding-v1",
        knowledge_chunk_size=256,
        knowledge_chunk_overlap=32,
    )
    resolver = ArtifactVersionResolver(
        settings,
        knowledge_paths=[knowledge],
        code_revision="abc123",
    )

    first = resolver.current(dataset)
    second = resolver.current(dataset)

    assert first == second
    assert first.chat_model == "mock"
    assert first.classifier_model == "classifier-v1"
    assert first.embedding_model == "embedding-v1"
    assert first.code_revision == "abc123"
    assert first.dataset_version == hashlib.sha256(dataset.read_bytes()).hexdigest()
    assert len(first.prompt_version) == 64
    assert len(first.index_version) == 64
    assert first.to_dict() == {
        "chatModel": "mock",
        "classifierModel": "classifier-v1",
        "promptVersion": first.prompt_version,
        "embeddingModel": "embedding-v1",
        "rerankerModel": "",
        "indexVersion": first.index_version,
        "datasetVersion": first.dataset_version,
        "calibrationVersion": "",
        "codeRevision": "abc123",
    }


def test_dataset_and_index_versions_change_only_with_their_inputs(tmp_path):
    knowledge = tmp_path / "knowledge.md"
    knowledge.write_text("版本一", encoding="utf-8")
    dataset = tmp_path / "eval.jsonl"
    dataset.write_text("one\n", encoding="utf-8")
    resolver = ArtifactVersionResolver(
        Settings(ai_provider="mock", knowledge_chunk_size=128),
        knowledge_paths=[knowledge],
        code_revision="fixed",
    )

    original = resolver.current(dataset)
    dataset.write_text("two\n", encoding="utf-8")
    dataset_changed = resolver.current(dataset)
    knowledge.write_text("版本二", encoding="utf-8")
    index_changed = resolver.current(dataset)

    assert dataset_changed.dataset_version != original.dataset_version
    assert dataset_changed.index_version == original.index_version
    assert index_changed.dataset_version == dataset_changed.dataset_version
    assert index_changed.index_version != dataset_changed.index_version
