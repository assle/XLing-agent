"""Public support-background API behavior during the domain expand phase."""

from app.api.routes import router
from app.core.security import hash_password
from app.models.entities import UserAccount, UserProfile
from tests.support import ApiHarness

_harness = ApiHarness(router)
_Session = _harness.sessions
client = _harness.client


def _seed_user() -> None:
    db = _Session()
    try:
        user = UserAccount(
            username="support-profile-user",
            display_name="Support Profile User",
            password_hash=hash_password("support-profile-password"),
        )
        user.roles = {"ROLE_USER"}
        db.add(user)
        db.commit()
    finally:
        db.close()


_seed_user()


def _token() -> str:
    response = client.post(
        "/api/auth/login",
        json={"username": "support-profile-user", "password": "support-profile-password"},
    )
    return response.json()["accessToken"]


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token()}"}


def _clean_profiles() -> None:
    db = _Session()
    try:
        db.query(UserProfile).filter(UserProfile.user_id == 1).delete()
        db.commit()
    finally:
        db.close()


def test_user_can_save_and_read_a_general_support_background():
    _clean_profiles()

    saved = client.put(
        "/api/profile/support",
        headers=_auth(),
        json={
            "currentConcern": "最近工作压力很大，晚上总是睡不好",
            "supportGoal": "先把睡眠和压力稳定下来",
            "preferredSupportStyle": "small_steps",
        },
    )

    assert saved.status_code == 200
    assert saved.json() == {
        "currentConcern": "最近工作压力很大，晚上总是睡不好",
        "supportGoal": "先把睡眠和压力稳定下来",
        "preferredSupportStyle": "small_steps",
    }

    loaded = client.get("/api/profile/support", headers=_auth())

    assert loaded.status_code == 200
    assert loaded.json() == saved.json()


def test_support_background_is_optional_and_requires_authentication():
    _clean_profiles()

    anonymous = client.get("/api/profile/support")
    authenticated = client.get("/api/profile/support", headers=_auth())

    assert anonymous.status_code == 401
    assert authenticated.status_code == 200
    assert authenticated.json() == {
        "currentConcern": None,
        "supportGoal": None,
        "preferredSupportStyle": None,
    }
