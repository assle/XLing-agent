from pathlib import Path

from app.core.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_default_reply_limit_is_large_enough_for_complete_support_responses():
    settings = Settings(_env_file=None)

    assert settings.ai_max_tokens >= 2048


def test_container_receives_the_configured_reply_limit():
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "AI_MAX_TOKENS: ${AI_MAX_TOKENS:-2048}" in compose


def test_mysql_healthcheck_uses_the_configured_root_password():
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "-proot" not in compose
    assert '$${MYSQL_ROOT_PASSWORD}' in compose
