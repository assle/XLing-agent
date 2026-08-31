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
    response = client.post(
        "/api/auth/reset",
        json={
            "username": "legacy-auth-user",
            "oldPassword": "legacy-password",
            "newPassword": "new-password",
        },
    )

    assert response.status_code == 404
