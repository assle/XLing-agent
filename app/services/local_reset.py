"""Offline reset of local conversation data for the LangGraph cutover."""
from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.entities import (
    ActionPlan,
    ActionPlanItem,
    AlertRecord,
    ChatMessage,
    ChatSession,
    CheckIn,
    CheckpointDeletionTask,
    DeadLetterRecord,
    ExcelRecord,
    KnowledgeChunk,
    MemoryCard,
    ReviewRequest,
    RiskTrajectoryPoint,
    SafetyAssessmentRecord,
    ScreeningResult,
    ToolJob,
    UserAccount,
    UserProfile,
)


class ResetCache(Protocol):
    def ping(self) -> Any: ...
    def delete(self, *keys: str) -> Any: ...
    def exists(self, *keys: str) -> Any: ...


class ResetRefused(RuntimeError):
    """A fixed, non-sensitive reset reason suitable for command output."""


@dataclass
class LocalResetResult:
    completed: bool = False
    deleted: dict[str, int] = field(default_factory=dict)
    remaining: dict[str, int] = field(default_factory=dict)
    preserved_before: dict[str, int] = field(default_factory=dict)
    preserved_after: dict[str, int] = field(default_factory=dict)
    cache_keys_selected: int = 0
    cache_keys_removed: int = 0
    cache_keys_remaining: int | None = None
    files_removed: list[str] = field(default_factory=list)
    files_remaining: list[str] | None = None
    errors: dict[str, str] = field(default_factory=dict)


def _is_loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def validate_local_targets(settings: Settings) -> None:
    """Reject remote database/cache targets without displaying connection secrets."""
    sql = make_url(settings.database_url)
    if sql.get_backend_name() not in {"sqlite", "mysql"}:
        raise ResetRefused("unsupported-local-database")
    if sql.get_backend_name() != "sqlite" and not _is_loopback(sql.host):
        raise ResetRefused("database-is-not-loopback")
    redis = urlparse(settings.redis_url)
    if redis.scheme not in {"redis", "rediss"} or not _is_loopback(redis.hostname):
        raise ResetRefused("cache-is-not-loopback")
    if settings.langgraph_checkpoint_backend not in {"sqlite", "async_sqlite", "memory"}:
        raise ResetRefused("unsupported-local-checkpoint")


def local_app_processes() -> list[int]:
    """Check native application processes and the local Compose app container.

    Background workers execute inside these application processes. A stopped
    worker flag is deliberately insufficient: its containing process must exit.
    No process is stopped by this operation.
    """
    output = subprocess.run(
        ["ps", "-ax", "-o", "pid=,command="], check=True, capture_output=True, text=True,
        timeout=10,
    ).stdout
    processes = []
    for line in output.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2 or int(parts[0]) == os.getpid():
            continue
        command = parts[1]
        if "app.main:app" in command or "app.services.tool_queue" in command or "scripts/run-dev.sh" in command:
            processes.append(int(parts[0]))
    if shutil.which("docker"):
        # Force the local context; a configured remote Docker context is not evidence
        # that a local app has stopped.
        context = subprocess.run(
            ["docker", "context", "inspect", "default", "--format", "{{json .Endpoints.docker.Host}}"],
            check=True, capture_output=True, text=True, timeout=10,
        )
        endpoint = urlparse(json.loads(context.stdout))
        if endpoint.scheme == "unix" and endpoint.path and not Path(endpoint.path).exists():
            return processes
        docker = subprocess.run(
            ["docker", "--context", "default", "compose", "ps", "--status", "running", "--quiet", "app"],
            cwd=get_settings().project_root, check=True, capture_output=True, text=True,
            timeout=10,
        )
        if docker.stdout.strip():
            processes.append(-1)  # A running container is a blocking writer too.
    return processes


def _path(settings: Settings, value: str) -> Path:
    path = Path(value).expanduser()
    return path.absolute() if path.is_absolute() else (settings.project_root / path).absolute()


def _preserved_counts(db: Session) -> dict[str, int]:
    counts = {
        model.__tablename__: db.query(model).count()
        for model in (UserAccount, UserProfile, MemoryCard, ScreeningResult, KnowledgeChunk)
    }
    independent_plans = db.query(ActionPlan.id).filter(ActionPlan.session_id.is_(None))
    counts.update({
        "independent_action_plans": independent_plans.count(),
        "independent_action_plan_items": db.query(ActionPlanItem).filter(ActionPlanItem.plan_id.in_(independent_plans)).count(),
        "independent_check_ins": db.query(CheckIn).filter(CheckIn.plan_id.in_(independent_plans)).count(),
        "independent_risk_trajectory": db.query(RiskTrajectoryPoint).filter(RiskTrajectoryPoint.session_id.is_(None)).count(),
    })
    return counts


def reset_local_conversations(
    settings: Settings,
    db: Session,
    cache: ResetCache,
    *,
    app_processes: Callable[[], list[int]] = local_app_processes,
) -> LocalResetResult:
    """Reset retired conversations and verify every store while retaining accounts.

    SQL only commits after selected cache keys and retired files are removed and
    verified. A failed stage rolls SQL back, leaves retirement identifiers available
    for another offline attempt, and explicitly returns an incomplete result.
    """
    result = LocalResetResult()
    stage = "preflight"
    try:
        validate_local_targets(settings)
        bind = db.get_bind()
        bound_url = bind.url if hasattr(bind, "url") else bind.engine.url
        if bound_url.get_backend_name() != "sqlite" and not _is_loopback(bound_url.host):
            raise ResetRefused("connected-database-is-not-loopback")
        if app_processes():
            raise ResetRefused("application-processes-still-running")
        if not cache.ping():
            raise ResetRefused("cache-ping-failed")
        result.preserved_before = _preserved_counts(db)
        thread_ids = {row.public_id for row in db.query(ChatSession).all()}
        for task in db.query(CheckpointDeletionTask).all():
            retired = json.loads(task.thread_ids_json)
            if not isinstance(retired, list) or any(not isinstance(item, str) for item in retired):
                raise ResetRefused("invalid-retired-thread-ids")
            thread_ids.update(retired)
        keys = [key for thread_id in sorted(thread_ids) for key in (
            f"xling:short-term-memory:{thread_id}", f"xling:cbt-state:{thread_id}",
        )]
        checkpoint = _path(settings, settings.langgraph_checkpoint_path)
        ledger = _path(settings, settings.excel_path)
        ledger_files = {ledger, *(_path(settings, row.file_path) for row in db.query(ExcelRecord).all())}
        if any(path.suffix.lower() != ".xlsx" or path.parent.resolve() != ledger.parent.resolve() for path in ledger_files):
            raise ResetRefused("ledger-is-outside-local-export-scope")
        if checkpoint.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
            raise ResetRefused("checkpoint-is-not-sqlite-file")
        files = {checkpoint, Path(f"{checkpoint}-wal"), Path(f"{checkpoint}-shm"), *ledger_files}
        preserved_folders = [
            _path(settings, settings.chroma_persist_dir),
            _path(settings, settings.finetuned_model_dir),
            settings.project_root / "models",
            settings.project_root / "knowledge",
        ]
        if any(path.resolve().is_relative_to(folder.resolve()) for path in files for folder in preserved_folders):
            raise ResetRefused("retired-file-is-preserved-asset")
        database_file = bound_url.database
        sql_path = _path(settings, str(database_file)) if bound_url.get_backend_name() == "sqlite" and database_file not in {None, "", ":memory:"} else None
        if sql_path and any(path.resolve() == sql_path.resolve() for path in files):
            raise ResetRefused("retired-file-is-business-database")
        if any(path.is_dir() for path in files):
            raise ResetRefused("retired-file-is-directory")

        linked_plans = db.query(ActionPlan.id).filter(ActionPlan.session_id.is_not(None))
        targets: list[tuple[Any, Any]] = [
            (CheckIn, CheckIn.plan_id.in_(linked_plans)),
            (ActionPlanItem, ActionPlanItem.plan_id.in_(linked_plans)),
            (ActionPlan, ActionPlan.session_id.is_not(None)),
            (RiskTrajectoryPoint, RiskTrajectoryPoint.session_id.is_not(None)),
        ]
        targets.extend((model, True) for model in (
            ReviewRequest, AlertRecord, ExcelRecord, DeadLetterRecord, ToolJob,
            SafetyAssessmentRecord, ChatMessage, ChatSession, CheckpointDeletionTask,
        ))
        stage = "sql"
        deleted = {model.__tablename__: db.query(model).filter(clause).delete(synchronize_session=False) for model, clause in targets}
        stage = "cache"
        result.cache_keys_selected = len(keys)
        if keys:
            result.cache_keys_removed = int(cache.delete(*keys))
        result.cache_keys_remaining = int(cache.exists(*keys)) if keys else 0
        if result.cache_keys_remaining:
            raise ResetRefused("retired-cache-remains")
        stage = "files"
        for path in sorted(files):
            existed = path.exists() or path.is_symlink()
            path.unlink(missing_ok=True)
            if existed:
                result.files_removed.append(str(path))
        result.files_remaining = [str(path) for path in sorted(files) if path.exists() or path.is_symlink()]
        if result.files_remaining:
            raise ResetRefused("retired-files-remain")
        stage = "verification"
        result.remaining = {model.__tablename__: db.query(model).filter(clause).count() for model, clause in targets}
        result.preserved_after = _preserved_counts(db)
        if any(result.remaining.values()) or result.preserved_before != result.preserved_after:
            raise ResetRefused("sql-scope-verification-failed")
        if app_processes():
            raise ResetRefused("application-process-restarted")
        stage = "commit"
        db.commit()
        result.deleted = deleted
        stage = "verification"
        result.remaining = {model.__tablename__: db.query(model).filter(clause).count() for model, clause in targets}
        result.preserved_after = _preserved_counts(db)
        if any(result.remaining.values()) or result.preserved_before != result.preserved_after:
            raise ResetRefused("committed-scope-verification-failed")
        result.completed = True
    except Exception as exc:
        result.errors[stage] = str(exc) if isinstance(exc, ResetRefused) else type(exc).__name__
        try:
            db.rollback()
        except Exception as rollback_error:
            result.errors["rollback"] = type(rollback_error).__name__
    return result


def main() -> int:
    """Run the explicitly scoped offline reset and print verifiable store results."""
    from redis import Redis

    result = LocalResetResult()
    stage = "preflight"
    try:
        settings = get_settings()
        validate_local_targets(settings)
        engine = create_engine(settings.database_url)
        cache = Redis.from_url(
            settings.redis_url, socket_timeout=settings.redis_socket_timeout_seconds,
            socket_connect_timeout=settings.redis_socket_timeout_seconds,
        )
        try:
            stage = "session"
            with Session(engine) as db:
                result = reset_local_conversations(settings, db, cache)
        finally:
            stage = "close"
            cache.close()
            engine.dispose()
    except Exception as exc:
        result.completed = False
        result.errors[stage] = str(exc) if isinstance(exc, ResetRefused) else type(exc).__name__
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    return 0 if result.completed else 1


if __name__ == "__main__":
    raise SystemExit(main())
