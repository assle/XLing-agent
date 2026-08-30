from pathlib import Path

from sqlalchemy import Engine, inspect, text
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
    bind = engine if engine is not None else default_engine
    Base.metadata.create_all(bind=bind)
    _migrate_review_action_columns(bind)
    _migrate_tool_job_uniqueness(bind)


def _migrate_review_action_columns(bind: Engine) -> None:
    """Add issue-5 review action fields to databases created by older builds."""
    columns = {
        "referral_target": "VARCHAR(200)",
        "next_step": "VARCHAR(500)",
        "follow_up_owner": "VARCHAR(128)",
        "follow_up_at": "DATETIME",
    }
    existing = {column["name"] for column in inspect(bind).get_columns("review_requests")}
    missing = [(name, ddl) for name, ddl in columns.items() if name not in existing]
    if not missing:
        return
    with bind.begin() as connection:
        for name, ddl in missing:
            connection.execute(text(f"ALTER TABLE review_requests ADD COLUMN {name} {ddl}"))


def _migrate_tool_job_uniqueness(bind: Engine) -> None:
    inspector = inspect(bind)
    if "tool_jobs" not in inspector.get_table_names():
        return
    constrained = False
    for item in [
        *inspector.get_unique_constraints("tool_jobs"),
        *inspector.get_indexes("tool_jobs"),
    ]:
        column_names = item.get("column_names")
        if (
            item.get("unique", True)
            and isinstance(column_names, (list, tuple))
            and set(column_names) == {"report_id", "kind"}
        ):
            constrained = True
            break
    if constrained:
        return
    with bind.begin() as connection:
        duplicate = connection.execute(text(
            """
            SELECT report_id, kind, COUNT(*) AS copies
            FROM tool_jobs
            GROUP BY report_id, kind
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        )).first()
        if duplicate is not None:
            raise RuntimeError(
                "tool_jobs 存在重复的 report_id + kind，无法添加唯一约束"
            )
        connection.execute(text(
            "CREATE UNIQUE INDEX uq_tool_jobs_report_kind ON tool_jobs(report_id, kind)"
        ))


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
