"""Tests for bcrypt + 24h JWT login.

Covers:
  - bcrypt password hashing and verification
  - JWT creation, decoding, expiry, tampering
  - Login endpoint (bcrypt credentials -> JWT)
  - Protected endpoints reject missing/tampered/expired tokens
  - Basic Auth no longer works
  - Role isolation (student can't access admin, admin can't chat)

Run: python -m pytest tests/test_security.py
"""
from __future__ import annotations

import jwt as pyjwt

from app.api.routes import router
from app.core.config import get_settings
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from app.models.entities import UserAccount
from tests.support import ApiHarness

# ---------------------------------------------------------------------------
# Test database + app setup
# ---------------------------------------------------------------------------

_harness = ApiHarness(router)
_TestSession = _harness.sessions
client = _harness.client


def _seed_users():
    db = _TestSession()
    try:
        student = UserAccount(
            username="student",
            display_name="Test Student",
            password_hash=hash_password("student123"),
        )
        student.roles = {"ROLE_USER"}

        admin = UserAccount(
            username="admin",
            display_name="Test Admin",
            password_hash=hash_password("admin123"),
        )
        admin.roles = {"ROLE_ADMIN", "ROLE_USER"}

        db.add_all([student, admin])
        db.commit()
    finally:
        db.close()


_seed_users()


def _reset_db():
    db = _TestSession()
    try:
        db.query(UserAccount).delete()
        db.commit()
    finally:
        db.close()
    _seed_users()


def _student_token():
    r = client.post("/api/auth/login", json={"username": "student", "password": "student123"})
    return r.json()["accessToken"]


# ---------------------------------------------------------------------------
# Unit: bcrypt password hashing
# ---------------------------------------------------------------------------

def test_bcrypt_hash_and_verify():
    hashed = hash_password("mypassword")
    assert hashed.startswith("$2")
    assert verify_password("mypassword", hashed)
    assert not verify_password("wrongpassword", hashed)


def test_bcrypt_hash_is_different_each_time():
    h1 = hash_password("same")
    h2 = hash_password("same")
    assert h1 != h2
    assert verify_password("same", h1)
    assert verify_password("same", h2)


# ---------------------------------------------------------------------------
# Unit: JWT creation and decoding
# ---------------------------------------------------------------------------

def test_jwt_create_and_decode():
    db = _TestSession()
    try:
        user = db.query(UserAccount).filter(UserAccount.username == "student").first()
        token = create_access_token(user)
        payload = decode_access_token(token)
        assert payload["sub"] == str(user.id)
        assert payload["username"] == "student"
        assert "ROLE_USER" in payload["roles"]
    finally:
        db.close()


def test_jwt_tampered_token_rejected():
    db = _TestSession()
    try:
        user = db.query(UserAccount).filter(UserAccount.username == "student").first()
        token = create_access_token(user)
        tampered = token[:-5] + "XXXXX"
        try:
            decode_access_token(tampered)
            assert False, "Should have raised"
        except Exception:
            pass
    finally:
        db.close()


def test_jwt_expired_token_rejected():
    settings = get_settings()
    expired_payload = {
        "sub": "1", "username": "student", "roles": ["ROLE_USER"],
        "exp": 1, "iat": 1,
    }
    expired_token = pyjwt.encode(
        expired_payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm
    )
    try:
        decode_access_token(expired_token)
        assert False, "Should have raised"
    except Exception:
        pass


def test_jwt_wrong_secret_rejected():
    wrong_token = pyjwt.encode(
        {"sub": "1", "username": "student", "roles": ["ROLE_USER"],
         "exp": 9999999999, "iat": 1},
        "wrong-secret-that-is-at-least-32-bytes", algorithm="HS256",
    )
    try:
        decode_access_token(wrong_token)
        assert False, "Should have raised"
    except Exception:
        pass


# ---------------------------------------------------------------------------
# API: login endpoint
# ---------------------------------------------------------------------------

def test_login_bcrypt_returns_jwt():
    response = client.post("/api/auth/login", json={"username": "student", "password": "student123"})
    assert response.status_code == 200
    data = response.json()
    assert "accessToken" in data
    assert data["tokenType"] == "Bearer"
    assert data["expiresIn"] == 86400


def test_login_admin_returns_jwt():
    response = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert response.status_code == 200
    data = response.json()
    assert data["accessToken"]


def test_login_wrong_password_401():
    response = client.post("/api/auth/login", json={"username": "student", "password": "wrong"})
    assert response.status_code == 401


def test_login_nonexistent_user_401():
    response = client.post("/api/auth/login", json={"username": "nobody", "password": "x"})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# API: protected endpoints reject bad tokens
# ---------------------------------------------------------------------------

def test_missing_token_rejected():
    response = client.get("/api/profile")
    assert response.status_code == 401


def test_basic_auth_no_longer_works():
    import base64
    cred = base64.b64encode(b"student:student123").decode()
    response = client.get("/api/profile", headers={"Authorization": f"Basic {cred}"})
    assert response.status_code == 401


def test_tampered_token_rejected():
    response = client.get("/api/profile", headers={"Authorization": "Bearer invalid.token.here"})
    assert response.status_code == 401


def test_valid_token_accepted():
    token = _student_token()
    response = client.get("/api/profile", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "student"


# ---------------------------------------------------------------------------
# API: role isolation
# ---------------------------------------------------------------------------

def test_student_cannot_access_admin():
    token = _student_token()
    response = client.get("/api/admin/reports", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


def test_admin_cannot_chat():
    response = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    admin_token = response.json()["accessToken"]
    response = client.post("/api/chat/stream", json={"message": "hello"}, headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
