import tempfile
from pathlib import Path

from tests.support import make_test_settings


def test_default_test_settings_ignore_personal_env_file(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "AI_PROVIDER=openai",
                "OPENAI_API_KEY=personal-key",
                "DATABASE_URL=mysql+pymysql://personal:personal@127.0.0.1:3306/personal",
                "CLASSIFIER_BACKEND=decision_head",
                "KNOWLEDGE_VECTOR_ENABLED=true",
                "TOOL_QUEUE_ENABLED=true",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    settings = make_test_settings()

    assert settings.environment == "test"
    assert settings.ai_provider == "mock"
    assert settings.database_url == "sqlite://"
    assert settings.classifier_backend == "ollama"
    assert settings.langgraph_checkpoint_backend == "memory"
    assert settings.knowledge_vector_enabled is False
    assert settings.tool_queue_enabled is False
    temporary_root = Path(tempfile.gettempdir()).resolve()
    for value in (
        settings.chroma_persist_dir,
        settings.excel_path,
        settings.langgraph_checkpoint_path,
        settings.diagnostic_log_dir,
    ):
        assert Path(value).resolve().is_relative_to(temporary_root)


def test_test_settings_preserve_explicit_overrides(tmp_path):
    settings = make_test_settings(
        ai_provider="ollama",
        knowledge_vector_enabled=True,
        tool_queue_enabled=True,
        diagnostic_log_dir=str(tmp_path / "diagnostics"),
    )

    assert settings.ai_provider == "ollama"
    assert settings.knowledge_vector_enabled is True
    assert settings.tool_queue_enabled is True
    assert settings.diagnostic_log_dir == str(tmp_path / "diagnostics")
