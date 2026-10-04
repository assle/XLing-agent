"""Authentication behavior after removing the legacy-password migration path."""

import hashlib

from app.api.routes import router
from app.core.security import hash_password
from app.models.entities import UserAccount
from tests.support import ApiHarness

_harness = ApiHarness(router)
_Session = _harness.sessions
client = _harness.client


def _seed_accounts() -> None:
    """准备一个当前密码格式账户和一个旧格式账户。

    保留两种已知密码，验证登录不再接受旧算法结果。
    """
    db = _Session()
    try:
        current = UserAccount(
            username="current-auth-user",
            display_name="Current Auth User",
            password_hash=hash_password("current-password"),
        )
        legacy = UserAccount(
            username="legacy-auth-user",
            display_name="Legacy Auth User",
            password_hash=hashlib.sha256(b"legacy-password").hexdigest(),
        )
        db.add_all([current, legacy])
        db.commit()
    finally:
        db.close()


_seed_accounts()


def test_login_accepts_only_the_current_password_hash_format():
    """分别提交两种密码格式账户的正确原密码。

    当前格式须登录成功并有凭证，旧格式须被拒绝。
    """
    current = client.post(
        "/api/auth/login",
        json={"username": "current-auth-user", "password": "current-password"},
    )
    legacy = client.post(
        "/api/auth/login",
        json={"username": "legacy-auth-user", "password": "legacy-password"},
    )

    assert current.status_code == 200
    assert current.json()["accessToken"]
    assert legacy.status_code == 401


def test_legacy_reset_endpoint_is_not_exposed():
    """请求旧密码迁移重置地址。

    检查返回 404，确认当前接口集合不再暴露该入口。
    """
    response = client.post(
        "/api/auth/reset",
        json={
            "username": "legacy-auth-user",
            "oldPassword": "legacy-password",
            "newPassword": "new-password",
        },
    )

    assert response.status_code == 404
