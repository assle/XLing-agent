from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.models.entities import UserAccount


def hash_password(password: str) -> str:
    """把原始密码转换为适合存入数据库的密码校验值。

    password 是用户输入的原始密码；返回文本形式的 bcrypt 校验值（用于核对密码的单向计算结果）。
    每次生成随机盐值，使相同密码也能得到不同结果；计算强度由配置决定。
    """
    import bcrypt
    settings = get_settings()
    # 先将文本转成算法需要的字节，再把结果转回文本，便于数据库存储。
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt(rounds=settings.bcrypt_rounds)
    ).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """核对输入密码与数据库保存的密码校验值是否匹配。

    password 是待验证的原始密码，hashed 是已保存的校验值；匹配时返回 True。
    仅将校验过程中出现的 ValueError、TypeError 转为 False；未捕获的异常仍向上传递。
    """
    import bcrypt
    try:
        # 校验库负责按照已有校验值中的参数核对密码，无需还原原始密码。
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(user: UserAccount) -> str:
    """为已确认身份的用户生成带签名的登录凭证。

    user 提供用户编号、名称和角色；返回 JWT（携带用户信息并带有签名的文本凭证）。
    有效期、签名密钥和算法均从配置读取；此处不再次验证密码，也不写入数据库。
    """
    settings = get_settings()
    # 统一使用世界标准时间计算过期时间，时长以配置中的分钟数为准。
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_access_token_expire_minutes)
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "roles": user.roles,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    """校验登录凭证的签名与有效期，并取出其中的数据。

    token 是客户端提交的凭证文本；通过校验后返回包含用户信息的字典。
    过期与无效凭证分别转为 401 错误（请求尚未通过身份验证），供接口统一响应。
    """
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")


def _bearer_token(request: Request) -> str:
    """从请求头提取 Bearer 格式的登录凭证。

    Bearer 表示请求携带登录凭证；格式前缀不区分大小写，返回第一个空格之后的文本。
    缺少该前缀时拒绝请求；提取出的文本是否有效由后续解码步骤判断。
    """
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing Bearer authorization")
    return header.split(" ", 1)[1]


def current_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> UserAccount:
    """根据请求中的登录凭证查询当前仍然存在的用户。

    request 提供请求头，db 是本次请求使用的数据库连接会话；返回用户记录。
    先验证凭证再按用户编号查询，避免仅凭凭证中的旧信息认定账户仍然有效。
    """
    token = _bearer_token(request)
    payload = decode_access_token(token)
    user_id = int(payload["sub"])
    # 重新读取用户记录，使后续权限判断依据当前数据库中的角色。
    user = db.get(UserAccount, user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    return user


def require_admin(user: Annotated[UserAccount, Depends(current_user)]) -> UserAccount:
    """检查当前用户是否具有管理员角色。

    user 由身份验证流程提供；权限通过时返回同一用户对象，便于接口继续使用。
    缺少管理员角色时抛出 403 错误，表示身份已知但无权执行该操作。
    """
    if "ROLE_ADMIN" not in user.roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    return user
