from pathlib import Path

from app.core.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_default_reply_limit_is_large_enough_for_complete_support_responses():
    """忽略本地环境文件构造默认配置。

    检查回复长度上限至少为 2048，不验证真实模型必然生成完整回复。
    """
    settings = Settings(_env_file=None)

    assert settings.ai_max_tokens >= 2048


def test_container_receives_the_configured_reply_limit():
    """读取容器配置文本。

    核对回复上限由环境变量传入并保留默认值，不实际启动容器。
    """
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "AI_MAX_TOKENS: ${AI_MAX_TOKENS:-2048}" in compose


def test_mysql_healthcheck_uses_the_configured_root_password():
    """检查数据库健康命令使用配置密码而非固定 root 密码。

    依据配置文字断言，不建立真实数据库连接。
    """
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "-proot" not in compose
    assert '$${MYSQL_ROOT_PASSWORD}' in compose
