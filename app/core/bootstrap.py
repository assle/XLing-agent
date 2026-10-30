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
    """创建缺失的数据表，并补齐旧数据库中的审核字段和任务唯一性约束。

    engine 可指定独立数据库；未传入时使用应用的默认连接。评估代码可传入测试数据库，避免触及正式数据。
    """
    bind = engine if engine is not None else default_engine
    Base.metadata.create_all(bind=bind)
    _migrate_review_action_columns(bind)
    _migrate_columns(bind, "chat_messages", {
        "review_id": "INTEGER",
        "client_request_id": "VARCHAR(64)",
    })
    _migrate_columns(bind, "alert_records", {
        "notification_kind": "VARCHAR(32) NOT NULL DEFAULT 'RISK_ALERT'",
    })
    _migrate_client_request_uniqueness(
        bind, "chat_messages", ("review_id", "role", "client_request_id"),
        "uq_chat_message_client_request",
    )
    _migrate_client_request_uniqueness(
        bind, "offsite_contact_records", ("review_id", "client_request_id"),
        "uq_offsite_contact_client_request",
    )
    _migrate_tool_job_uniqueness(bind)


def _migrate_review_action_columns(bind: Engine) -> None:
    """给旧版审核请求表补上当前需要的处理详情和负责人字段。"""
    _migrate_columns(bind, "review_requests", {
        "referral_target": "VARCHAR(200)",
        "next_step": "VARCHAR(500)",
        "follow_up_owner": "VARCHAR(128)",
        "follow_up_at": "DATETIME",
        "completion_basis": "TEXT",
        "claimed_by": "VARCHAR(64)",
        "claimed_at": "DATETIME",
        "escalation_count": "INTEGER NOT NULL DEFAULT 0",
        "last_escalated_at": "DATETIME",
    })


def _migrate_columns(bind: Engine, table: str, columns: dict[str, str]) -> None:
    """仅给已有旧表添加缺失列，不改变已有数据或其他表结构。"""
    inspector = inspect(bind)
    if table not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns(table)}
    missing = [(name, ddl) for name, ddl in columns.items() if name not in existing]
    if not missing:
        return
    # 在数据库管理的事务范围内执行结构变更；具体结构变更的回滚能力取决于数据库。
    with bind.begin() as connection:
        for name, ddl in missing:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def _migrate_client_request_uniqueness(
    bind: Engine, table: str, columns: tuple[str, ...], index_name: str,
) -> None:
    """给旧表补请求编号唯一索引；空编号仍允许保存多条合法记录。"""
    inspector = inspect(bind)
    if table not in inspector.get_table_names():
        return
    constraints = [*inspector.get_unique_constraints(table), *inspector.get_indexes(table)]
    if any(
        item.get("unique", True) and set(item.get("column_names") or []) == set(columns)
        for item in constraints
    ):
        return
    names = ", ".join(columns)
    non_null = " AND ".join(f"{name} IS NOT NULL" for name in columns if name != "role")
    with bind.begin() as connection:
        duplicate = connection.execute(text(
            f"SELECT {names}, COUNT(*) AS copies FROM {table} "
            f"WHERE {non_null} GROUP BY {names} HAVING COUNT(*) > 1 LIMIT 1"
        )).first()
        if duplicate is not None:
            raise RuntimeError(f"{table} 存在重复的客户端请求编号，无法添加唯一约束")
        connection.execute(text(f"CREATE UNIQUE INDEX {index_name} ON {table}({names})"))


def _migrate_tool_job_uniqueness(bind: Engine) -> None:
    """确保同一安全评估记录的同类工具任务不能重复登记。

    先检查现有唯一约束和索引；缺少约束时先查重复数据，再建立联合唯一索引。
    已有重复记录时抛错并要求处理数据，不擅自删除或合并任务。
    """
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
        # 先确认旧数据满足唯一性要求，避免带着重复任务建立约束。
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
    """在空账户库中创建演示账户，并加载随应用提供的知识资料。

    db 是写入使用的数据库会话，settings 可覆盖默认配置；账户创建会确认保存。
    仅在账户总数为零时建立演示账户；知识来源逐个交给知识服务检查和补充。
    """
    settings = settings or get_settings()
    if db.query(UserAccount).count() == 0:
        admin = UserAccount(
            username="admin",
            display_name="Authorized Reviewer",
            password_hash=hash_password("admin123"),
        )
        admin.roles = {"ROLE_ADMIN", "ROLE_USER"}
        user = UserAccount(
            username="student",
            display_name="Demo User",
            password_hash=hash_password("student123"),
        )
        user.roles = {"ROLE_USER"}
        db.add_all([admin, user])
        db.commit()

    service = KnowledgeService(db, settings)
    root = Path(__file__).resolve().parents[1]
    for file in sorted((root / "knowledge").glob("*.md")):
        service.ensure_source(file.name, file.read_text(encoding="utf-8"))
