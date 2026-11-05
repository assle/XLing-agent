import json
import os
import shutil
import subprocess
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


def test_local_launcher_preserves_env_file_model_overrides(tmp_path):
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    shutil.copy2(PROJECT_ROOT / "scripts/run-dev.sh", script_dir / "run-dev.sh")
    (tmp_path / ".env").write_text(
        "AI_PROVIDER=openai\n"
        "OLLAMA_BASE_URL=http://env-file:11434\n"
        "OLLAMA_MODEL=env-file-model\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uvicorn = bin_dir / "uvicorn"
    uvicorn.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "values = dict(line.split('=', 1) for line in Path('.env').read_text().splitlines())\n"
        "keys = ('AI_PROVIDER', 'OLLAMA_BASE_URL', 'OLLAMA_MODEL')\n"
        "effective = {key: os.environ[key] if key in os.environ else values[key] for key in keys}\n"
        "print(json.dumps({'arguments': sys.argv[1:], 'effective': effective}))\n",
        encoding="utf-8",
    )
    uvicorn.chmod(0o755)
    env = os.environ.copy()
    for key in ("AI_PROVIDER", "OLLAMA_BASE_URL", "OLLAMA_MODEL"):
        env.pop(key, None)
    env.update(PATH=f"{bin_dir}:{env['PATH']}", SERVER_HOST="0.0.0.0", SERVER_PORT="9090")

    result = subprocess.run(
        [str(script_dir / "run-dev.sh")],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    launched = json.loads(result.stdout)

    assert launched["effective"] == {
        "AI_PROVIDER": "openai",
        "OLLAMA_BASE_URL": "http://env-file:11434",
        "OLLAMA_MODEL": "env-file-model",
    }
    assert launched["arguments"][-4:] == ["--host", "0.0.0.0", "--port", "9090"]


def test_local_template_only_overrides_the_demo_provider(monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
        monkeypatch.delenv(name.lower(), raising=False)

    defaults = Settings(_env_file=None)
    from_template = Settings(_env_file=PROJECT_ROOT / ".env.example")

    assert from_template.ai_provider == "mock"
    for name in Settings.model_fields:
        if name != "ai_provider":
            assert getattr(from_template, name) == getattr(defaults, name), name


def test_compose_does_not_duplicate_application_model_defaults(tmp_path):
    config = _compose_config(tmp_path, "models")

    assert "AI_PROVIDER" not in config["services"]["app"]["environment"]
    registration = config["services"]["ollama-init"]["environment"]
    for key in ("AI_PROVIDER", "OLLAMA_MODEL", "OLLAMA_CLASSIFIER_MODEL"):
        assert registration[key] == ""


def _compose_config(tmp_path, *profiles):
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose is required for rendered configuration tests")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "JWT_SECRET_KEY=runtime-test-signing-key\n"
        "MYSQL_DATABASE=runtime_config_test\n"
        "MYSQL_USER=runtime_user\n"
        "MYSQL_PASSWORD=runtime-password\n"
        "MYSQL_ROOT_PASSWORD=runtime-root-password\n"
        "MYSQL_PORT=13307\n",
        encoding="utf-8",
    )
    compose_file = tmp_path / "docker-compose.yml"
    shutil.copy2(PROJECT_ROOT / "docker-compose.yml", compose_file)
    command = ["docker", "compose"]
    for profile in profiles:
        command.extend(["--profile", profile])
    command.extend(["--env-file", str(env_file), "-f", str(compose_file), "config", "--format", "json"])
    result = subprocess.run(command, cwd=tmp_path, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def test_compose_adapts_mysql_for_the_app_healthcheck_and_local_tools(tmp_path):
    config = _compose_config(tmp_path)
    mysql = config["services"]["mysql"]
    app = config["services"]["app"]

    assert mysql["environment"]["MYSQL_DATABASE"] == "runtime_config_test"
    assert mysql["environment"]["MYSQL_USER"] == "runtime_user"
    assert mysql["environment"]["MYSQL_PASSWORD"] == "runtime-password"
    assert app["environment"]["DATABASE_URL"] == (
        "mysql+pymysql://runtime_user:runtime-password@mysql:3306/runtime_config_test?charset=utf8mb4"
    )
    assert mysql["healthcheck"]["test"] == [
        "CMD-SHELL",
        'mysqladmin ping -h 127.0.0.1 -uroot -p"$${MYSQL_ROOT_PASSWORD}" --silent',
    ]
    port = mysql["ports"][0]
    assert port["host_ip"] == "127.0.0.1"
    assert port["published"] == "13307"
    assert port["target"] == 3306


def test_mysql_test_profile_uses_an_isolated_ephemeral_database(tmp_path):
    default_config = _compose_config(tmp_path)
    test_config = _compose_config(tmp_path, "test")

    assert "mysql-test" not in default_config["services"]
    mysql = test_config["services"]["mysql-test"]
    assert mysql["environment"]["MYSQL_DATABASE"] == "xling_test"
    assert mysql["tmpfs"] == ["/var/lib/mysql"]
    assert mysql["ports"][0]["host_ip"] == "127.0.0.1"
    assert mysql["ports"][0]["published"] == "13306"
    assert test_config["services"]["tests"]["environment"]["DATABASE_URL"] == (
        "mysql+pymysql://xling_test:test-only@mysql-test:3306/xling_test?charset=utf8mb4"
    )


def test_compose_does_not_inject_proxy_settings(tmp_path):
    config = _compose_config(tmp_path, "models", "test")
    proxy_variables = {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}

    for service in config["services"].values():
        environment_keys = {key.lower() for key in service.get("environment", {})}
        assert proxy_variables.isdisjoint(environment_keys)
        build_args = service.get("build", {}).get("args", {})
        assert proxy_variables.isdisjoint({key.lower() for key in build_args})
