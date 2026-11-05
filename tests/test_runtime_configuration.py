from pathlib import Path

from app.core.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_default_reply_limit_is_large_enough_for_complete_support_responses():
    """忽略本地环境文件构造默认配置。

    检查回复长度上限至少为 2048，不验证真实模型必然生成完整回复。
    """
    settings = Settings(_env_file=None)

    assert settings.ai_max_tokens >= 2048


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
