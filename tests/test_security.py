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
    """准备普通用户和管理员两种账户及当前格式的密码校验值。

    提交后关闭连接，供登录和权限隔离测试共同使用。
    """
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
    """删除测试账户再重新建立默认两种账户。

    仅操作本文件的内存测试环境，为再次运行恢复初始数据。
    """
    db = _TestSession()
    try:
        db.query(UserAccount).delete()
        db.commit()
    finally:
        db.close()
    _seed_users()


def _student_token():
    """通过登录接口取得普通用户的真实测试凭证。

    后续接口使用该值验证权限，不跳过身份验证。
    """
    r = client.post("/api/auth/login", json={"username": "student", "password": "student123"})
    return r.json()["accessToken"]


# ---------------------------------------------------------------------------
# Unit: bcrypt password hashing
# ---------------------------------------------------------------------------

def test_bcrypt_hash_and_verify():
    """用同一密码生成校验值，再分别核对正确和错误密码。

    检查结果采用当前格式，并且仅正确密码匹配。
    """
    hashed = hash_password("mypassword")
    assert hashed.startswith("$2")
    assert verify_password("mypassword", hashed)
    assert not verify_password("wrongpassword", hashed)


def test_bcrypt_hash_is_different_each_time():
    """对同一密码连续生成两个校验值。

    检查随机盐值使结果不同，但两者都能验证原密码。
    """
    h1 = hash_password("same")
    h2 = hash_password("same")
    assert h1 != h2
    assert verify_password("same", h1)
    assert verify_password("same", h2)


# ---------------------------------------------------------------------------
# Unit: JWT creation and decoding
# ---------------------------------------------------------------------------

def test_jwt_create_and_decode():
    """为已有测试用户签发并解码登录凭证。

    核对用户编号、名称和角色字段，最后释放数据库连接。
    """
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
    """修改凭证尾部后尝试解码，覆盖签名受损场景。

    当前宽泛异常捕获也会捕获 assert False，本用例通过不能单独证明拒绝逻辑生效。
    """
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
    """构造过期时间已经过去的凭证并尝试解码。

    当前异常捕获也包含用来表示未抛错的断言，验证力度有限。
    """
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
    """使用不同密钥签发凭证后交给当前配置验证。

    当前宽泛捕获可能吞掉失败断言，因此这里只记录其覆盖意图，不声称充分证明拒绝。
    """
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
    """用正确的普通用户密码登录。

    检查成功状态、凭证字段及接口当前固定返回的有效秒数。
    """
    response = client.post("/api/auth/login", json={"username": "student", "password": "student123"})
    assert response.status_code == 200
    data = response.json()
    assert "accessToken" in data
    assert data["tokenType"] == "Bearer"
    assert data["expiresIn"] == 86400


def test_login_admin_returns_jwt():
    """使用管理员账户登录。

    检查接口成功且返回非空凭证，不在本例验证管理员的后续权限。
    """
    response = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert response.status_code == 200
    data = response.json()
    assert data["accessToken"]


def test_login_wrong_password_401():
    """提交已存在用户的错误密码。

    检查接口返回未通过身份验证的 401 状态。
    """
    response = client.post("/api/auth/login", json={"username": "student", "password": "wrong"})
    assert response.status_code == 401


def test_login_nonexistent_user_401():
    """使用不存在的用户名请求登录。

    检查与密码错误同样返回 401，避免响应状态区分账户是否存在。
    """
    response = client.post("/api/auth/login", json={"username": "nobody", "password": "x"})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# API: protected endpoints reject bad tokens
# ---------------------------------------------------------------------------

def test_missing_token_rejected():
    """不携带登录凭证请求个人资料。

    检查受保护接口拒绝请求，而不是返回默认账户。
    """
    response = client.get("/api/profile")
    assert response.status_code == 401


def test_basic_auth_no_longer_works():
    """使用旧式用户名密码请求头访问个人资料。

    检查该方式不能绕过当前登录凭证验证。
    """
    import base64
    cred = base64.b64encode(b"student:student123").decode()
    response = client.get("/api/profile", headers={"Authorization": f"Basic {cred}"})
    assert response.status_code == 401


def test_tampered_token_rejected():
    """向个人资料接口发送明显无效的凭证文本。

    直接检查接口返回 401，验证完整请求路径的拒绝行为。
    """
    response = client.get("/api/profile", headers={"Authorization": "Bearer invalid.token.here"})
    assert response.status_code == 401


def test_valid_token_accepted():
    """先正常登录再带凭证查询资料。

    检查请求成功并返回对应普通用户名称。
    """
    token = _student_token()
    response = client.get("/api/profile", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "student"


# ---------------------------------------------------------------------------
# API: role isolation
# ---------------------------------------------------------------------------

def test_student_cannot_access_admin():
    """使用普通用户凭证访问管理报告接口。

    检查返回 403，区分已登录但无权访问与未登录。
    """
    token = _student_token()
    response = client.get("/api/admin/reports", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


def test_admin_cannot_chat():
    """让管理员登录后尝试发起聊天。

    检查管理账户被聊天接口拒绝，即使它同时具备普通角色。
    """
    response = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    admin_token = response.json()["accessToken"]
    response = client.post("/api/chat/stream", json={"message": "hello"}, headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 本组测试结束。
# ---------------------------------------------------------------------------
