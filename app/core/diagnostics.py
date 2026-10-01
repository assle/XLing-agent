"""Metadata-only execution records for local, single-process debugging."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4

from langgraph.errors import GraphBubbleUp

from app.core.config import Settings

_FIELDS = frozenset({
    "session_id", "thread_id", "user_message_id", "assistant_message_id", "message_id",
    "report_id", "review_id", "job_id", "attempt", "original_run_id", "origin_run_id",
    "route", "risk_level", "risk_source", "requires_review", "review_action",
    "interview_completed", "interview_stage", "retrieval_count", "reason_code", "model",
    "provider", "job_kind", "status", "backend", "checkpoint_id", "wait_ms", "streamed",
    "record_count", "tool", "dependency_job_id", "reused", "recovered_count", "decision",
    "intent", "quick_risk_flagged",
})
_context: ContextVar[tuple[str, dict[str, object]] | None] = ContextVar("diagnostic_execution", default=None)
_stage_start: ContextVar[float | None] = ContextVar("diagnostic_stage_start", default=None)
_lock = threading.RLock()
_handler: RotatingFileHandler | None = None
_path: Path | None = None
_configured = False


def _notice() -> None:
    try:
        print("[diagnostics] output unavailable", file=sys.stderr)
    except Exception:
        pass


class _SafeFileHandler(RotatingFileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        _notice()


def configure(settings: Settings) -> None:
    """Initialize the fixed rotation policy, without making logging a startup gate."""
    global _handler, _path, _configured
    log_dir = Path(settings.diagnostic_log_dir)
    if not log_dir.is_absolute():
        log_dir = settings.project_root / log_dir
    path = log_dir / "execution.jsonl"
    with _lock:
        if _configured and _path == path and _handler is not None:
            return
        shutdown()
        _configured = True
        _path = path
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            _handler = _SafeFileHandler(path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
            _handler.setFormatter(logging.Formatter("%(message)s"))
        except Exception:
            _notice()


def shutdown() -> None:
    global _handler, _path, _configured
    with _lock:
        if _handler is not None:
            try:
                _handler.close()
            except Exception:
                _notice()
        _handler = None
        _path = None
        _configured = False


def _metadata(values: dict[str, object]) -> dict[str, object]:
    return {
        key: value for key, value in values.items()
        if key in _FIELDS and (value is None or type(value) in (str, bool, int, float))
    }


def _error(exception: BaseException) -> dict[str, object]:
    frames = []
    tb = exception.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        frames.append({"file": Path(code.co_filename).name, "function": code.co_name, "line": tb.tb_lineno})
        tb = tb.tb_next
    return {"error_type": type(exception).__name__, "frames": frames}


def _write(event: str, metadata: dict[str, object], **details: object) -> None:
    if not _configured:
        return
    context = _context.get()
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": context[0] if context else None,
        "event": event,
        **(context[1] if context else {}),
        **_metadata(metadata),
        **details,
    }
    with _lock:
        try:
            message = json.dumps(record, ensure_ascii=False, allow_nan=False)
            if _handler is not None:
                _handler.handle(logging.LogRecord("xling.diagnostics", logging.INFO, "", 0, message, (), None))
        except Exception:
            _notice()
        try:
            summary = f"[diagnostics] run={record['run_id']} {event}"
            for key in ("reason_code", "error_type", "duration_ms", "job_id", "attempt"):
                if key in record:
                    summary += f" {key}={record[key]}"
            print(summary)
        except Exception:
            _notice()


def emit(event: str, **metadata: object) -> None:
    _write(event, metadata)


def degraded(operation: str, reason_code: str, exception: BaseException | None = None, **metadata: object) -> None:
    """Describe a caught failure without rendering its value, request or locals."""
    started = _stage_start.get()
    details = _error(exception) if exception is not None else {}
    if started is not None:
        details["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
    _write(f"{operation}.degraded", {**metadata, "reason_code": reason_code}, **details)


def current_run_id() -> str | None:
    context = _context.get()
    return context[0] if context else None


def bind(**metadata: object) -> None:
    """Attach known identifiers to the active execution, if one exists."""
    context = _context.get()
    if context is not None:
        _context.set((context[0], {**context[1], **_metadata(metadata)}))


@dataclass(frozen=True)
class Trace:
    run_id: str

    def bind(self, **metadata: object) -> None:
        context = _context.get()
        if context is not None and context[0] == self.run_id:
            bind(**metadata)


@contextmanager
def stage(operation: str, **metadata: object) -> Iterator[None]:
    started = time.monotonic()
    token = _stage_start.set(started)
    _write(f"{operation}.start", metadata)
    try:
        yield
    except BaseException as exc:
        outcome = "interrupted" if isinstance(exc, GraphBubbleUp) else (
            "cancelled" if isinstance(exc, (asyncio.CancelledError, GeneratorExit)) else "failed"
        )
        _write(
            f"{operation}.{outcome}", metadata,
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            **(_error(exc) if outcome == "failed" else {}),
        )
        raise
    else:
        _write(f"{operation}.end", metadata, duration_ms=round((time.monotonic() - started) * 1000, 3))
    finally:
        _stage_start.reset(token)


@contextmanager
def execution(operation: str, **metadata: object) -> Iterator[Trace]:
    trace = Trace(uuid4().hex)
    token = _context.set((trace.run_id, _metadata(metadata)))
    try:
        with stage(operation):
            yield trace
    finally:
        _context.reset(token)
