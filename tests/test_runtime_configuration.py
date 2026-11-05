from pathlib import Path

import pytest

from app.core.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_default_reply_limit_is_large_enough_for_complete_support_responses():
    """忽略本地环境文件构造默认配置。

    检查回复长度上限至少为 2048，不验证真实模型必然生成完整回复。
    """
    settings = Settings(_env_file=None)

    assert settings.ai_max_tokens >= 2048


def test_environment_mode_defaults_to_local_and_accepts_test_and_production():
    assert Settings(_env_file=None).environment == "local"
    assert Settings(_env_file=None, environment="test").environment == "test"
    assert Settings(
        _env_file=None,
        environment="production",
        jwt_secret_key="production-only-signing-key",
        database_url="mysql+pymysql://xling:production-only-password@db/xling",
    ).environment == "production"


def test_environment_variables_override_application_defaults(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("AI_MAX_TOKENS", "4096")

    settings = Settings(_env_file=None)

    assert settings.environment == "test"
    assert settings.ai_max_tokens == 4096


def test_embedding_settings_expose_one_fallback_rule():
    fallback = Settings(
        _env_file=None,
        openai_base_url="https://answer.example/v1",
        openai_api_key="answer-key",
    )
    override = Settings(
        _env_file=None,
        openai_base_url="https://answer.example/v1",
        openai_api_key="answer-key",
        embedding_base_url="https://embedding.example/v1",
        embedding_api_key="embedding-key",
    )

    assert fallback.effective_embedding_base_url == "https://answer.example/v1"
    assert fallback.effective_embedding_api_key == "answer-key"
    assert override.effective_embedding_base_url == "https://embedding.example/v1"
    assert override.effective_embedding_api_key == "embedding-key"


def test_relative_paths_resolve_from_project_root_and_absolute_paths_stay_unchanged():
    settings = Settings(_env_file=None)
    absolute = Path("/tmp/xling-settings/model")

    assert settings.resolve_path("data/chroma") == PROJECT_ROOT / "data/chroma"
    assert settings.resolve_path(absolute) == absolute
    assert settings.resolve_path("~/models") == Path.home() / "models"


def test_decision_head_bundle_resolves_one_fixed_artifact_layout(tmp_path):
    bundle_dir = tmp_path / "decision-head"
    settings = Settings(_env_file=None, decision_head_bundle_dir=str(bundle_dir))

    paths = settings.decision_head_paths

    assert paths.bundle_dir == bundle_dir
    assert paths.base_dir == bundle_dir / "base"
    assert paths.adapter_dir == bundle_dir / "adapter"
    assert paths.head_path == bundle_dir / "head.pt"
    assert paths.calibration_path == bundle_dir / "calibration.json"
    assert paths.system_prompt_path == bundle_dir / "system-prompt.txt"
    assert Settings(_env_file=None).decision_head_bundle_dir == "models/xling-decision-head"


@pytest.mark.parametrize(
    "database_url",
    ["not-a-database-url", "mysql+pymysql://app:password@db:not-a-port/xling"],
)
def test_malformed_database_url_fails_settings_construction(database_url):
    with pytest.raises(ValueError, match="database_url"):
        Settings(_env_file=None, database_url=database_url)


@pytest.mark.parametrize(
    "overrides",
    [
        {"classifier_high_risk_threshold": -0.01},
        {"classifier_high_risk_threshold": 1.01},
        {"classifier_high_risk_review_threshold": -0.01},
        {"classifier_low_confidence_threshold": 1.01},
        {"classifier_margin_threshold": -0.01},
        {"classifier_high_risk_threshold": 0.5, "classifier_high_risk_review_threshold": 0.6},
    ],
)
def test_classifier_probability_configuration_is_validated(overrides):
    with pytest.raises(ValueError, match="classifier_"):
        Settings(_env_file=None, **overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"knowledge_chunk_size": 0},
        {"knowledge_chunk_overlap": -1},
        {"knowledge_chunk_size": 100, "knowledge_chunk_overlap": 100},
        {"knowledge_chunk_size": 100, "knowledge_chunk_overlap": 101},
    ],
)
def test_knowledge_chunk_overlap_must_be_smaller_than_chunk_size(overrides):
    with pytest.raises(ValueError, match="knowledge_chunk"):
        Settings(_env_file=None, **overrides)


@pytest.mark.parametrize(
    ("overrides", "field_name"),
    [
        ({"smtp_use_ssl": True}, "smtp_use_tls"),
        ({"alert_email_delivery_mode": "smtp"}, "smtp_host"),
        ({"smtp_username": "mailer@example.com"}, "smtp_password"),
        ({"smtp_password": "mail-password"}, "smtp_username"),
    ],
)
def test_smtp_transport_and_credentials_must_be_consistent(overrides, field_name):
    with pytest.raises(ValueError, match=field_name):
        Settings(_env_file=None, **overrides)


def test_openai_provider_requires_an_api_key():
    with pytest.raises(ValueError, match="openai_api_key"):
        Settings(_env_file=None, ai_provider="openai")

    settings = Settings(_env_file=None, ai_provider="openai", openai_api_key="configured-key")
    assert settings.ai_provider == "openai"


@pytest.mark.parametrize(
    ("jwt_secret_key", "database_url", "field_name"),
    [
        (
            "xling-dev-secret-change-in-production",
            "mysql+pymysql://app:safe-password@db/xling",
            "jwt_secret_key",
        ),
        ("", "mysql+pymysql://app:safe-password@db/xling", "jwt_secret_key"),
        (
            "production-only-signing-key",
            "mysql+pymysql://xling:xling@db/xling",
            "database_url",
        ),
        (
            "production-only-signing-key",
            "mysql+pymysql://app@db/xling",
            "database_url",
        ),
    ],
)
def test_production_rejects_missing_or_development_secrets(jwt_secret_key, database_url, field_name):
    with pytest.raises(ValueError, match=field_name):
        Settings(
            _env_file=None,
            environment="production",
            jwt_secret_key=jwt_secret_key,
            database_url=database_url,
        )


def test_secrets_and_database_credentials_are_hidden_from_configuration_output():
    settings = Settings(
        _env_file=None,
        openai_api_key="openai-secret-marker",
        embedding_api_key="embedding-secret-marker",
        smtp_username="mailer@example.com",
        smtp_password="smtp-secret-marker",
        jwt_secret_key="jwt-secret-marker",
        database_url="mysql+pymysql://app:database-secret-marker@db.example/xling",
    )

    assert settings.openai_api_key.get_secret_value() == "openai-secret-marker"
    assert settings.embedding_api_key.get_secret_value() == "embedding-secret-marker"
    assert settings.smtp_password.get_secret_value() == "smtp-secret-marker"
    assert settings.jwt_secret_key.get_secret_value() == "jwt-secret-marker"
    assert settings.safe_database_url == "mysql+pymysql://app:***@db.example/xling"
    representations = (
        str(settings),
        repr(settings),
        repr(settings.model_dump()),
        settings.model_dump_json(),
    )
    for representation in representations:
        assert "openai-secret-marker" not in representation
        assert "embedding-secret-marker" not in representation
        assert "smtp-secret-marker" not in representation
        assert "jwt-secret-marker" not in representation
        assert "database-secret-marker" not in representation


def test_container_loads_local_settings_from_the_shared_env_file():
    """应用容器统一读取本地配置，示例文件保留回复上限默认值。"""
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "env_file: .env" in compose
    assert "AI_MAX_TOKENS=2048" in example


def test_mysql_healthcheck_uses_the_configured_root_password():
    """检查数据库健康命令使用配置密码而非固定 root 密码。

    依据配置文字断言，不建立真实数据库连接。
    """
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "-proot" not in compose
    assert '$${MYSQL_ROOT_PASSWORD}' in compose


def test_mysql_is_available_to_local_database_tools_only_on_loopback():
    """DBeaver 等本机工具使用可配置端口，数据库不绑定到公开网卡。"""
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    assert '"127.0.0.1:${MYSQL_PORT:-3306}:3306"' in compose
    assert "MYSQL_PORT=3306" in example
    assert "MYSQL_DATABASE=xling" in example
    assert "MYSQL_USER=xling" in example
    assert "MYSQL_PASSWORD=xling" in example


def test_project_configuration_does_not_pin_proxy_settings():
    """代理只允许通过单次命令临时提供，不写入项目配置文件。"""
    paths = [
        PROJECT_ROOT / ".env.example",
        PROJECT_ROOT / "docker-compose.yml",
        PROJECT_ROOT / "scripts/closing/compose.override.yml",
    ]
    for path in paths:
        content = path.read_text(encoding="utf-8").lower()
        for variable in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
            assert f"{variable}=" not in content
            assert f"{variable}:" not in content
