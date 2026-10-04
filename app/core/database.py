from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine_kwargs: dict[str, object] = {
    "pool_pre_ping": True,
    "pool_recycle": 3600,
}
if settings.database_url.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(
    settings.database_url,
    **engine_kwargs,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    """为一次接口调用提供数据库会话，并在调用结束后关闭。

    yield 将会话交给框架管理的调用方；无论正常返回还是抛错，finally 都负责释放连接。
    此函数不自动确认保存，写入是否提交由具体业务决定。
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
