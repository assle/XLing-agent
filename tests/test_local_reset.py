from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings
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
from app.services.local_reset import reset_local_conversations


class LocalCache:
    def __init__(self) -> None:
        self.keys = {
            "xling:short-term-memory:old-session": "old conversation",
            "xling:cbt-state:old-session": "old questioning",
            "unrelated-cache": "keep",
        }

    def ping(self) -> bool:
        return True

    def delete(self, *keys: str) -> int:
        removed = sum(key in self.keys for key in keys)
        for key in keys:
            self.keys.pop(key, None)
        return removed

    def exists(self, *keys: str) -> int:
        return sum(key in self.keys for key in keys)


def seed_conversations(db: Session) -> None:
    user = UserAccount(username="retained-account", display_name="retained", password_hash="hash")
    db.add(user)
    db.flush()
    conversation = ChatSession(public_id="old-session", title="old", user_id=user.id)
    db.add(conversation)
    db.flush()
    linked_plan = ActionPlan(user_id=user.id, session_id=conversation.id)
    independent_plan = ActionPlan(user_id=user.id, session_id=None)
    db.add_all([linked_plan, independent_plan])
    db.flush()
    db.add_all([
        ChatMessage(user_id=user.id, session_id=conversation.id, role="user", content="old message"),
        ActionPlanItem(plan_id=linked_plan.id, content="old action", order_index=0),
        ActionPlanItem(plan_id=independent_plan.id, content="retained action", order_index=0),
        CheckIn(plan_id=linked_plan.id, user_id=user.id, items_snapshot_json="[]", improvement_status="better"),
        CheckIn(plan_id=independent_plan.id, user_id=user.id, items_snapshot_json="[]", improvement_status="better"),
        RiskTrajectoryPoint(user_id=user.id, session_id=conversation.id, risk_level="LOW", risk_score=0.1),
        RiskTrajectoryPoint(user_id=user.id, session_id=None, risk_level="LOW", risk_score=0.1),
        UserProfile(user_id=user.id, current_concern="retained background"),
        MemoryCard(user_id=user.id, content="retained memory", confirmed=True),
        ScreeningResult(user_id=user.id, scale_type="PHQ9", answers_json="[]", total_score=0, severity="none"),
        KnowledgeChunk(source="retained", source_index=0, content="retained knowledge"),
    ])
    db.commit()


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_url="sqlite://",
        langgraph_checkpoint_path=str(tmp_path / "checkpoints.db"),
        excel_path=str(tmp_path / "risk-ledger.xlsx"),
    )


def test_offline_reset_removes_old_conversations_and_retains_independent_data(database_harness, tmp_path):
    settings = settings_for(tmp_path)
    checkpoint = Path(settings.langgraph_checkpoint_path)
    files = [checkpoint, Path(f"{checkpoint}-wal"), Path(f"{checkpoint}-shm"), Path(settings.excel_path)]
    for path in files:
        path.write_text("retired state")
    retained_file = tmp_path / "knowledge.txt"
    retained_file.write_text("keep")
    cache = LocalCache()
    with database_harness.sessions() as db:
        seed_conversations(db)
        result = reset_local_conversations(settings, db, cache, app_processes=lambda: [])

        assert result.completed
        assert result.deleted["chat_sessions"] == 1
        assert result.deleted["action_plans"] == 1
        assert result.remaining["chat_sessions"] == 0
        assert result.preserved_before == result.preserved_after
        assert result.preserved_after == {
            "user_accounts": 1, "user_profiles": 1, "memory_cards": 1,
            "screening_results": 1, "knowledge_chunks": 1,
            "independent_action_plans": 1, "independent_action_plan_items": 1,
            "independent_check_ins": 1, "independent_risk_trajectory": 1,
        }
        assert all(not path.exists() for path in files)
        assert retained_file.read_text() == "keep"
        assert cache.keys == {"unrelated-cache": "keep"}


def test_reset_refuses_running_application_before_mutation(database_harness, tmp_path):
    settings = settings_for(tmp_path)
    Path(settings.langgraph_checkpoint_path).write_text("old checkpoint")
    cache = LocalCache()
    with database_harness.sessions() as db:
        seed_conversations(db)
        result = reset_local_conversations(settings, db, cache, app_processes=lambda: [123])
        assert not result.completed
        assert result.errors == {"preflight": "application-processes-still-running"}
        assert result.deleted == {}
        assert db.query(ChatSession).count() == 1
        assert cache.exists("xling:short-term-memory:old-session") == 1
        assert Path(settings.langgraph_checkpoint_path).read_text() == "old checkpoint"


def test_reset_retires_reviews_jobs_ledger_and_previously_deleted_session_cache(database_harness, tmp_path):
    settings = settings_for(tmp_path)
    cache = LocalCache()
    cache.keys["xling:cbt-state:deleted-account-session"] = "old pending cleanup"
    ledger = tmp_path / "previous-ledger.xlsx"
    ledger.write_text("old export")
    with database_harness.sessions() as db:
        seed_conversations(db)
        conversation = db.query(ChatSession).one()
        report = SafetyAssessmentRecord(
            user_id=conversation.user_id, session_id=conversation.id, content="old", intent="risk",
            emotion="negative", emotion_score=0.9, risk_level="HIGH", confidence=0.9, summary="old",
        )
        db.add(report)
        db.flush()
        job = ToolJob(report_id=report.id, kind="excel", status="running")
        db.add(job)
        db.flush()
        db.add_all([
            ReviewRequest(session_id=conversation.id, report_id=report.id, thread_id=conversation.public_id),
            AlertRecord(report_id=report.id, channel="email", recipient="local", status="pending", message="old"),
            ExcelRecord(report_id=report.id, file_path=str(ledger), status="SUCCESS", message="old"),
            DeadLetterRecord(job_id=job.id, report_id=report.id, kind="excel", reason="old"),
            CheckpointDeletionTask(checkpoint_path=settings.langgraph_checkpoint_path,
                                   thread_ids_json='["deleted-account-session"]'),
        ])
        db.commit()
        result = reset_local_conversations(settings, db, cache, app_processes=lambda: [])

        assert result.completed
        for name in ("psychological_reports", "review_requests", "alert_records", "excel_records",
                     "tool_jobs", "dead_letter_records", "checkpoint_deletion_tasks"):
            assert result.deleted[name] == 1
            assert result.remaining[name] == 0
        assert cache.keys == {"unrelated-cache": "keep"}
        assert not ledger.exists()


def test_cache_cleanup_failure_reports_incomplete_without_losing_retirement_ids(database_harness, tmp_path):
    class UnavailableCache(LocalCache):
        def delete(self, *keys: str) -> int:
            raise ConnectionError("synthetic secret must not enter output")

    settings = settings_for(tmp_path)
    Path(settings.langgraph_checkpoint_path).write_text("old checkpoint")
    cache = UnavailableCache()
    with database_harness.sessions() as db:
        seed_conversations(db)
        result = reset_local_conversations(settings, db, cache, app_processes=lambda: [])
        assert not result.completed
        assert result.errors == {"cache": "ConnectionError"}
        assert result.deleted == {}
        assert db.query(ChatSession).one().public_id == "old-session"
        assert Path(settings.langgraph_checkpoint_path).read_text() == "old checkpoint"


@pytest.mark.parametrize("target", ["database", "cache"])
def test_reset_refuses_nonlocal_storage_targets_without_exposing_credentials(database_harness, tmp_path, target):
    settings = settings_for(tmp_path)
    if target == "database":
        settings.database_url = "mysql+pymysql://user:synthetic-secret@remote.example/xling"
    else:
        settings.redis_url = "redis://:synthetic-secret@remote.example/0"
    with database_harness.sessions() as db:
        seed_conversations(db)
        result = reset_local_conversations(settings, db, LocalCache(), app_processes=lambda: [])
        assert not result.completed
        assert result.errors == {"preflight": f"{target}-is-not-loopback"}
        assert db.query(ChatSession).count() == 1


def test_reset_refuses_a_ledger_record_pointing_outside_the_local_export_scope(database_harness, tmp_path):
    settings = settings_for(tmp_path)
    retained = tmp_path / "knowledge.txt"
    retained.write_text("retained knowledge")
    with database_harness.sessions() as db:
        seed_conversations(db)
        db.add(ExcelRecord(report_id=123, file_path=str(retained), status="SUCCESS", message="old"))
        db.commit()
        result = reset_local_conversations(settings, db, LocalCache(), app_processes=lambda: [])
        assert not result.completed
        assert result.errors == {"preflight": "ledger-is-outside-local-export-scope"}
        assert retained.read_text() == "retained knowledge"
        assert db.query(ChatSession).count() == 1


def test_file_cleanup_failure_reports_incomplete_and_preserves_retirement_ids(database_harness, tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    checkpoint = Path(settings.langgraph_checkpoint_path)
    checkpoint.write_text("old checkpoint")
    unlink = Path.unlink

    def refuse_checkpoint(path: Path, missing_ok: bool = False) -> None:
        if path == checkpoint:
            raise PermissionError("synthetic file failure with secret")
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse_checkpoint)
    with database_harness.sessions() as db:
        seed_conversations(db)
        result = reset_local_conversations(settings, db, LocalCache(), app_processes=lambda: [])
        assert not result.completed
        assert result.errors == {"files": "PermissionError"}
        assert result.files_remaining is None
        assert db.query(ChatSession).one().public_id == "old-session"
        assert checkpoint.read_text() == "old checkpoint"


def test_sql_cleanup_failure_rolls_back_the_retired_conversation_scope(database_harness, tmp_path):
    cache = LocalCache()
    with database_harness.sessions() as db:
        seed_conversations(db)
        db.execute(text("CREATE TRIGGER refuse_reset BEFORE DELETE ON chat_sessions "
                        "BEGIN SELECT RAISE(ABORT, 'synthetic SQL failure'); END"))
        db.commit()
        result = reset_local_conversations(settings_for(tmp_path), db, cache, app_processes=lambda: [])
        assert not result.completed
        assert result.errors == {"sql": "IntegrityError"}
        assert result.deleted == {}
        assert db.query(ChatSession).one().public_id == "old-session"
        assert db.query(ActionPlan).count() == 2
        assert db.query(ActionPlanItem).count() == 2
        assert cache.exists("xling:short-term-memory:old-session") == 1


def test_reset_refuses_checkpoint_configuration_colliding_with_preserved_knowledge(database_harness, tmp_path):
    settings = settings_for(tmp_path)
    settings.chroma_persist_dir = str(tmp_path / "chroma")
    chroma_file = Path(settings.chroma_persist_dir) / "chroma.sqlite3"
    chroma_file.parent.mkdir()
    chroma_file.write_text("retained vector index")
    settings.langgraph_checkpoint_path = str(chroma_file)
    with database_harness.sessions() as db:
        seed_conversations(db)
        result = reset_local_conversations(settings, db, LocalCache(), app_processes=lambda: [])
        assert not result.completed
        assert result.errors == {"preflight": "retired-file-is-preserved-asset"}
        assert chroma_file.read_text() == "retained vector index"
        assert db.query(ChatSession).count() == 1


def test_offline_reset_command_reports_nonlocal_target_without_connection_secrets():
    environment = dict(os.environ, DATABASE_URL="mysql+pymysql://user:synthetic-secret@remote.example/xling")
    completed = subprocess.run(
        [sys.executable, "-m", "app.services.local_reset"], env=environment,
        capture_output=True, text=True, timeout=10,
    )
    result = json.loads(completed.stdout)
    assert completed.returncode == 1
    assert not result["completed"]
    assert result["errors"] == {"preflight": "database-is-not-loopback"}
    assert "synthetic-secret" not in completed.stdout + completed.stderr


def test_native_reset_can_run_with_installed_docker_and_absent_local_daemon_socket(
    database_harness, tmp_path, monkeypatch,
):
    docker_socket = tmp_path / "inactive-docker.sock"

    def local_commands(command: list[str], **kwargs):
        if command[0] == "ps":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[0] == "docker" and "inspect" in command:
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(f"unix://{docker_socket}"), stderr="",
            )
        raise subprocess.CalledProcessError(1, command, stderr="inactive Docker daemon")

    monkeypatch.setattr(shutil, "which", lambda command: "/usr/local/bin/docker")
    monkeypatch.setattr(subprocess, "run", local_commands)
    with database_harness.sessions() as db:
        seed_conversations(db)
        result = reset_local_conversations(settings_for(tmp_path), db, LocalCache())
        assert result.completed
        assert result.remaining["chat_sessions"] == 0
        assert result.preserved_before == result.preserved_after
