from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import Base
from app.core.database import engine as default_engine
from app.core.security import hash_password
from app.models.entities import UserAccount
from app.services.knowledge import KnowledgeService


def create_schema(engine: Engine | None = None) -> None:
    """Create all tables on the given engine (defaults to the app's global engine).

    The eval runner passes its own SQLite engine so it never touches MySQL.
    """
    Base.metadata.create_all(bind=engine if engine is not None else default_engine)


def seed_data(db: Session, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if db.query(UserAccount).count() == 0:
        admin = UserAccount(
            username="admin",
            display_name="Counselor Admin",
            password_hash=hash_password("admin123"),
        )
        admin.roles = {"ROLE_ADMIN", "ROLE_USER"}
        student = UserAccount(
            username="student",
            display_name="Demo Student",
            password_hash=hash_password("student123"),
        )
        student.roles = {"ROLE_USER"}
        db.add_all([admin, student])
        db.commit()

    service = KnowledgeService(db, settings)
    root = Path(__file__).resolve().parents[1]
    for file in sorted((root / "knowledge").glob("*.md")):
        service.ensure_source(file.name, file.read_text(encoding="utf-8"))
