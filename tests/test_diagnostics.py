from __future__ import annotations

import asyncio
import builtins
import json

import pytest
from langgraph.errors import GraphInterrupt

from app.core import diagnostics
from app.core.config import Settings


def test_request_can_be_traced_after_completion_and_restart(tmp_path, capsys):
    settings = Settings(diagnostic_log_dir=str(tmp_path))
    diagnostics.configure(settings)
    with diagnostics.execution("chat") as trace:
        trace.bind(session_id=12, thread_id="session-12")
        with diagnostics.stage("node.intent"):
            diagnostics.emit("branch.selected", route="CHAT", reason_code="message_classified")
    diagnostics.shutdown()
    diagnostics.configure(settings)
    diagnostics.shutdown()

    records = [json.loads(line) for line in (tmp_path / "execution.jsonl").read_text().splitlines()]
    assert [row["event"] for row in records] == [
        "chat.start", "node.intent.start", "branch.selected", "node.intent.end", "chat.end",
    ]
    assert {row["run_id"] for row in records} == {trace.run_id}
    assert records[-1]["session_id"] == 12
    assert records[-1]["duration_ms"] >= 0
    assert trace.run_id in capsys.readouterr().out


def test_failures_are_locatable_without_sensitive_exception_or_state(tmp_path, capsys):
    diagnostics.configure(Settings(diagnostic_log_dir=str(tmp_path)))
    marker = "PRIVATE-SYNTHETIC-CONTENT"
    failure = ValueError(marker)
    try:
        with pytest.raises(ValueError) as caught:
            with diagnostics.execution("chat", content=marker):
                with diagnostics.stage("node.intent", state={"secret": marker}):
                    raise failure
        assert caught.value is failure
    finally:
        diagnostics.shutdown()
    output = (tmp_path / "execution.jsonl").read_text()
    terminal = capsys.readouterr()
    assert marker not in output + terminal.out + terminal.err
    records = [json.loads(line) for line in output.splitlines()]
    failed = next(row for row in records if row["event"] == "node.intent.failed")
    assert failed["error_type"] == "ValueError"
    assert failed["frames"][-1]["function"] == "test_failures_are_locatable_without_sensitive_exception_or_state"
    assert set(failed["frames"][-1]) == {"file", "function", "line"}
    assert failed["duration_ms"] >= 0


@pytest.mark.parametrize("signal,outcome", [(GraphInterrupt(), "interrupted"), (asyncio.CancelledError(), "cancelled")])
def test_pause_and_cancel_are_distinct_and_propagate(tmp_path, signal, outcome):
    diagnostics.configure(Settings(diagnostic_log_dir=str(tmp_path)))
    try:
        with pytest.raises(type(signal)) as caught:
            with diagnostics.execution("chat"):
                with diagnostics.stage("node.review"):
                    raise signal
        assert caught.value is signal
    finally:
        diagnostics.shutdown()
    records = [json.loads(line) for line in (tmp_path / "execution.jsonl").read_text().splitlines()]
    assert records[-1]["event"] == f"chat.{outcome}"
    assert records[-2]["event"] == f"node.review.{outcome}"
    assert not any(row["event"].endswith("failed") for row in records)


def test_log_open_failure_does_not_change_business_result(tmp_path, capsys):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("occupied")
    diagnostics.configure(Settings(diagnostic_log_dir=str(blocked)))
    try:
        with diagnostics.execution("chat"):
            result = "business-result"
    finally:
        diagnostics.shutdown()
    assert result == "business-result"
    assert "output unavailable" in capsys.readouterr().err


def test_log_write_failure_preserves_result_and_never_renders_exception(tmp_path, capsys, monkeypatch):
    original_open = builtins.open
    marker = "PRIVATE-SYNTHETIC-FILESYSTEM-ERROR"

    class BrokenStream:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def write(self, message):
            raise OSError(marker)

    def failing_open(file, mode="r", *args, **kwargs):
        stream = original_open(file, mode, *args, **kwargs)
        return BrokenStream(stream) if str(file).endswith("execution.jsonl") and mode == "a" else stream

    monkeypatch.setattr(builtins, "open", failing_open)
    diagnostics.configure(Settings(diagnostic_log_dir=str(tmp_path)))
    try:
        with diagnostics.execution("chat"):
            result = "business-result"
    finally:
        diagnostics.shutdown()
    terminal = capsys.readouterr()
    assert result == "business-result"
    assert "output unavailable" in terminal.err
    assert marker not in terminal.out + terminal.err


def test_rotation_keeps_at_most_five_backup_files(tmp_path, capsys):
    diagnostics.configure(Settings(diagnostic_log_dir=str(tmp_path)))
    try:
        with diagnostics.execution("chat", thread_id="x" * (1024 * 1024)):
            for _ in range(61):
                diagnostics.emit("branch.selected", route="CHAT")
    finally:
        diagnostics.shutdown()
    files = list(tmp_path.glob("execution.jsonl*"))
    assert {path.name for path in files} == {
        "execution.jsonl", "execution.jsonl.1", "execution.jsonl.2",
        "execution.jsonl.3", "execution.jsonl.4", "execution.jsonl.5",
    }
    assert all(path.stat().st_size < 10 * 1024 * 1024 for path in files)


def test_async_executions_keep_their_own_association(tmp_path):
    diagnostics.configure(Settings(diagnostic_log_dir=str(tmp_path)))

    async def request(session_id):
        with diagnostics.execution("chat", session_id=session_id) as trace:
            await asyncio.sleep(0)
            diagnostics.emit("branch.selected", route="CHAT")
            return trace.run_id

    async def both():
        return await asyncio.gather(request(12), request(13))

    try:
        first, second = asyncio.run(both())
    finally:
        diagnostics.shutdown()
    records = [json.loads(line) for line in (tmp_path / "execution.jsonl").read_text().splitlines()]
    assert first != second
    assert {row["session_id"] for row in records if row["run_id"] == first} == {12}
    assert {row["session_id"] for row in records if row["run_id"] == second} == {13}
    assert diagnostics.current_run_id() is None


def test_caught_failure_has_reason_type_and_operation_timing(tmp_path):
    diagnostics.configure(Settings(diagnostic_log_dir=str(tmp_path)))
    try:
        with diagnostics.execution("chat"):
            with diagnostics.stage("assessment"):
                try:
                    raise RuntimeError("PRIVATE-SYNTHETIC-CONTENT")
                except RuntimeError as exc:
                    diagnostics.degraded("assessment", "classifier_unavailable", exc)
    finally:
        diagnostics.shutdown()
    records = [json.loads(line) for line in (tmp_path / "execution.jsonl").read_text().splitlines()]
    degraded = next(row for row in records if row["event"] == "assessment.degraded")
    assert degraded["reason_code"] == "classifier_unavailable"
    assert degraded["error_type"] == "RuntimeError"
    assert degraded["duration_ms"] >= 0
    assert records[-1]["event"] == "chat.end"


def test_unknown_classifier_response_is_a_safe_timed_degradation(tmp_path, capsys, caplog):
    from app.services.assessment import PsychologicalAssessmentService

    marker = "PRIVATE-SYNTHETIC-CLASSIFIER-RESPONSE"

    class Classifier:
        def classify(self, text):
            return marker

    diagnostics.configure(Settings(diagnostic_log_dir=str(tmp_path)))
    try:
        with diagnostics.execution("chat"):
            result = PsychologicalAssessmentService(Classifier()).assess("有点担心")
    finally:
        diagnostics.shutdown()
    output = (tmp_path / "execution.jsonl").read_text()
    terminal = capsys.readouterr()
    assert marker not in output + terminal.out + terminal.err + caplog.text
    records = [json.loads(line) for line in output.splitlines()]
    degraded = next(row for row in records if row["event"] == "assessment.degraded")
    assert degraded["reason_code"] == "unknown_classifier_label"
    assert degraded["duration_ms"] >= 0
    assert result.risk.value == "MEDIUM"
