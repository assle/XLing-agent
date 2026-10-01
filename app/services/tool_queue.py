from __future__ import annotations

import json
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy.orm import Session

from app.core import diagnostics
from app.core.config import Settings
from app.core.database import SessionLocal
from app.core.enums import RiskLevel, ToolJobKind, ToolJobStatus, ToolStatus
from app.core.time import utc_now
from app.models.entities import DeadLetterRecord, ExcelRecord, SafetyAssessmentRecord, ToolJob
from app.services.tools import ToolOrchestrationService


class ToolQueueService:
    def __init__(self, db: Session, settings: Settings):
        """保存任务队列使用的数据库会话和配置。

        初始化不登记或执行任务。
        """
        self.db = db
        self.settings = settings

    def enqueue_report(self, report_id: int, risk_level: str | None) -> list[ToolJob]:
        """为安全评估记录登记表格任务，高风险时再登记依赖它的通知任务。

        复用已有待执行、执行中或成功任务，最后统一提交并返回任务列表。
        """
        excel_job, excel_created = self._find_or_create(ToolJobKind.EXCEL_REPORT.value, report_id)
        jobs = [excel_job]
        registrations = [(excel_job, excel_created)]
        if risk_level == RiskLevel.HIGH.value:
            alert_job, alert_created = self._find_or_create(ToolJobKind.RISK_ALERT.value, report_id, excel_job.id)
            jobs.append(alert_job)
            registrations.append((alert_job, alert_created))
        self.db.commit()
        for job, created in registrations:
            diagnostics.emit(
                "tool_job.registered", report_id=report_id, job_id=job.id, job_kind=job.kind,
                dependency_job_id=job.depends_on_job_id, status=job.status, reused=not created,
            )
        return jobs

    def claim_job(self, job_id: int) -> bool:
        """尝试把已到执行时间的待处理任务改为执行中。

        将编号、状态和时间放在同一次条件更新中；仅更新到一行时返回 True，避免多个执行者同时领取同一待处理状态。
        """
        claimed = (
            self.db.query(ToolJob)
            .filter(
                ToolJob.id == job_id,
                ToolJob.status == ToolJobStatus.PENDING.value,
                ToolJob.run_after <= utc_now(),
            )
            .update(
                {
                    ToolJob.status: ToolJobStatus.RUNNING.value,
                    ToolJob.updated_at: utc_now(),
                },
                synchronize_session=False,
            )
        )
        self.db.commit()
        if claimed == 1:
            diagnostics.emit("tool_job.claimed", job_id=job_id)
        return claimed == 1

    def recover_expired_jobs(self) -> int:
        """把执行时间超过租约的任务重新排入待处理队列。

        租约是允许任务保持执行中状态的最长时间，至少一秒；此处不判断原执行线程是否仍在工作。
        """
        cutoff = utc_now() - timedelta(
            seconds=max(1.0, self.settings.tool_queue_lease_seconds)
        )
        recovered = (
            self.db.query(ToolJob)
            .filter(
                ToolJob.status == ToolJobStatus.RUNNING.value,
                ToolJob.updated_at < cutoff,
            )
            .update(
                {
                    ToolJob.status: ToolJobStatus.PENDING.value,
                    ToolJob.last_error: "任务租约过期，已重新排队",
                    ToolJob.run_after: utc_now(),
                    ToolJob.updated_at: utc_now(),
                },
                synchronize_session=False,
            )
        )
        self.db.commit()
        if recovered:
            diagnostics.emit("tool_job.lease_recovered", recovered_count=recovered)
        return recovered

    def _find_or_create(
        self, kind: str, report_id: int, depends_on_job_id: int | None = None,
    ) -> tuple[ToolJob, bool]:
        """查找可复用的同记录同类型任务，找不到则准备新任务。

        新任务只 flush 取得编号；已失败任务不在复用范围内，新建仍受数据库唯一约束限制。
        """
        existing = (
            self.db.query(ToolJob)
            .filter(ToolJob.report_id == report_id, ToolJob.kind == kind)
            .filter(ToolJob.status.in_([ToolJobStatus.PENDING.value, ToolJobStatus.RUNNING.value, ToolJobStatus.SUCCESS.value]))
            .first()
        )
        if existing is not None:
            return existing, False
        job = ToolJob(
            report_id=report_id,
            kind=kind,
            status=ToolJobStatus.PENDING.value,
            attempts=0,
            max_attempts=self.settings.tool_queue_max_attempts,
            depends_on_job_id=depends_on_job_id,
            run_after=utc_now(),
            last_error="",
        )
        self.db.add(job)
        self.db.flush()
        return job, True


class RateLimiter:
    def __init__(self, limit_per_minute: int):
        """初始化一分钟窗口内的调用次数限制和共享锁。

        非正限制值表示不限制，时间记录只保存在当前实例中。
        """
        self.limit = max(0, limit_per_minute)
        self.events: deque[float] = deque()
        self.lock = threading.Lock()

    def allow(self) -> tuple[bool, float]:
        """判断本次通知是否获准执行，并返回建议等待秒数。

        用单调时钟清理一分钟前的记录；允许时立即占用一次额度，超限时至少等待一秒。
        """
        if self.limit <= 0:
            return True, 0.0
        now_ts = time.monotonic()
        # 把移除过期计数、检查额度和占用额度放在同一锁内，避免并发超发。
        with self.lock:
            while self.events and now_ts - self.events[0] >= 60.0:
                self.events.popleft()
            if len(self.events) < self.limit:
                self.events.append(now_ts)
                return True, 0.0
            retry_after = max(1.0, 60.0 - (now_ts - self.events[0]))
            return False, retry_after


class ToolQueueWorker:
    def __init__(self, settings: Settings):
        """创建任务调度状态、表格与邮件线程池和邮件频率限制器。

        两类任务使用独立工作池，实际调度线程由 start 启动。
        """
        self.settings = settings
        self.stop_event = threading.Event()
        self.dispatcher: threading.Thread | None = None
        self.excel_executor = ThreadPoolExecutor(
            max_workers=max(1, settings.tool_queue_excel_workers),
            thread_name_prefix="xling-excel",
        )
        self.email_executor = ThreadPoolExecutor(
            max_workers=max(1, settings.tool_queue_email_workers),
            thread_name_prefix="xling-email",
        )
        self.email_limiter = RateLimiter(settings.alert_email_rate_limit_per_minute)

    def start(self) -> None:
        """在启用队列且尚未启动时恢复过期任务并启动调度线程。

        已有调度线程引用时直接返回，不重复创建后台循环。
        """
        if not self.settings.tool_queue_enabled or self.dispatcher is not None:
            return
        self._recover_running_jobs()
        self.dispatcher = threading.Thread(target=self._loop, name="xling-tool-dispatcher", daemon=True)
        self.dispatcher.start()

    def stop(self) -> None:
        """请求调度循环停止，并取消线程池中尚未开始的任务。

        最多等待调度线程五秒；线程池关闭不等待已运行任务完成，也不会强制终止它们。
        """
        self.stop_event.set()
        if self.dispatcher is not None:
            self.dispatcher.join(timeout=5)
        self.excel_executor.shutdown(wait=False, cancel_futures=True)
        self.email_executor.shutdown(wait=False, cancel_futures=True)

    def _loop(self) -> None:
        """持续执行任务分发，并在每轮后等待配置的间隔。

        单轮异常记录后继续下一轮；等待使用停止事件，可在收到停止请求时提前结束。
        """
        while not self.stop_event.is_set():
            try:
                self._dispatch_once()
            except Exception as exc:
                diagnostics.degraded("tool_queue.dispatch", "dispatch_failed", exc)
            self.stop_event.wait(self.settings.tool_queue_poll_interval_seconds)

    def _dispatch_once(self) -> None:
        """恢复过期任务、取一批已到期任务并尝试领取后提交到线程池。

        按创建时间优先处理较早任务；每轮单独创建并关闭数据库会话。
        """
        db = SessionLocal()
        try:
            ToolQueueService(db, self.settings).recover_expired_jobs()
            now = utc_now()
            jobs = (
                db.query(ToolJob)
                .filter(ToolJob.status == ToolJobStatus.PENDING.value, ToolJob.run_after <= now)
                .order_by(ToolJob.created_at.asc())
                .limit(self.settings.tool_queue_batch_size)
                .all()
            )
            for job in jobs:
                # 查询结果只是候选；领取时再次验证状态，失败说明任务已变化或被其他执行者领取。
                if not ToolQueueService(db, self.settings).claim_job(job.id):
                    continue
                executor = self.excel_executor if job.kind == ToolJobKind.EXCEL_REPORT.value else self.email_executor
                executor.submit(self._run_job, job.id)
        finally:
            db.close()

    def _run_job(self, job_id: int) -> None:
        """检查任务依赖和限流条件后执行一次工具操作并记录结果。

        等待依赖或额度时重新排队，不增加尝试次数；真正执行前先记录次数。
        执行失败尝试登记重试或失败留存，最后始终关闭本任务数据库会话。
        """
        with diagnostics.execution("tool_job.worker", job_id=job_id) as trace:
            db = SessionLocal()
            try:
                job = db.get(ToolJob, job_id)
                if job is None or job.status != ToolJobStatus.RUNNING.value:
                    return
                trace.bind(report_id=job.report_id, job_kind=job.kind, attempt=job.attempts)
                # 依赖未成功属于暂时不能执行，重新排队且不消耗一次工具尝试次数。
                if not self._dependency_ready(db, job):
                    self._requeue(db, job, "等待 Excel 台账写入成功后再发送预警", 2.0)
                    diagnostics.emit("tool_job.deferred", reason_code="dependency_pending")
                    return
                # 只有邮件通知需要频率限制，表格写入使用自己的线程池和文件锁。
                if job.kind == ToolJobKind.RISK_ALERT.value:
                    allowed, retry_after = self.email_limiter.allow()
                    if not allowed:
                        self._requeue(db, job, "邮件预警限流中，稍后重试", retry_after)
                        diagnostics.emit("tool_job.deferred", reason_code="notification_rate_limited")
                        return
                # 只有依赖和限流检查通过、即将实际执行时才消耗一次尝试次数。
                job.attempts += 1
                job.updated_at = utc_now()
                db.add(job)
                db.commit()
                trace.bind(attempt=job.attempts)
                with diagnostics.stage("tool_job.attempt"):
                    self._execute(db, job)
                job.status = ToolJobStatus.SUCCESS.value
                job.last_error = ""
                job.updated_at = utc_now()
                db.add(job)
                with diagnostics.stage("tool_job.save"):
                    db.commit()
                diagnostics.emit("tool_job.succeeded")
            except Exception as exc:
                diagnostics.degraded("tool_job.worker", "job_failed", exc)
                try:
                    self._fail_or_dead_letter(db, job_id, exc)
                except Exception as recording_error:
                    diagnostics.degraded("tool_job.failure_save", "failure_record_unavailable", recording_error, job_id=job_id)
            finally:
                with diagnostics.stage("tool_job.close"):
                    db.close()

    def _execute(self, db: Session, job: ToolJob) -> None:
        """根据任务种类调用表格写入或风险通知服务。

        安全评估记录缺失、类型未知或工具返回非成功状态均抛错，交由任务失败策略处理。
        """
        report = db.get(SafetyAssessmentRecord, job.report_id)
        if report is None:
            raise RuntimeError(f"report {job.report_id} not found")
        tools = ToolOrchestrationService(db, self.settings)
        if job.kind == ToolJobKind.EXCEL_REPORT.value:
            excel_record = tools.write_excel(report)
            if excel_record.status != ToolStatus.SUCCESS.value:
                raise RuntimeError(excel_record.message)
            return
        if job.kind == ToolJobKind.RISK_ALERT.value:
            alert_record = tools.notify(report)
            if alert_record.status != ToolStatus.SUCCESS.value:
                raise RuntimeError(alert_record.message)
            return
        raise RuntimeError(f"unknown tool job kind: {job.kind}")

    def _dependency_ready(self, db: Session, job: ToolJob) -> bool:
        """判断风险通知所依赖的表格写入是否成功。

        有前置任务编号时检查任务状态，否则查找成功的表格记录；其他任务直接允许执行。
        """
        if job.kind != ToolJobKind.RISK_ALERT.value:
            return True
        if job.depends_on_job_id:
            dependency = db.get(ToolJob, job.depends_on_job_id)
            return dependency is not None and dependency.status == ToolJobStatus.SUCCESS.value
        return (
            db.query(ExcelRecord)
            .filter(ExcelRecord.report_id == job.report_id, ExcelRecord.status == ToolStatus.SUCCESS.value)
            .first()
            is not None
        )

    def _requeue(self, db: Session, job: ToolJob, reason: str, delay_seconds: float) -> None:
        """将任务重新置为待处理并设置原因及下次可执行时间。

        delay_seconds 至少按一秒计算，保存后交由后续调度轮次领取。
        """
        job.status = ToolJobStatus.PENDING.value
        job.last_error = reason
        job.run_after = utc_now() + timedelta(seconds=max(1.0, delay_seconds))
        job.updated_at = utc_now()
        db.add(job)
        db.commit()

    def _fail_or_dead_letter(self, db: Session, job_id: int, exc: Exception) -> None:
        """根据已尝试次数决定再次排队或转入失败留存区。

        达到上限时保存失败详情；未达到时按尝试次数增加等待时间。
        此入口直接使用传入会话，数据库自身失败后的会话恢复不由它完成。
        """
        job = db.get(ToolJob, job_id)
        if job is None:
            return
        message = f"{type(exc).__name__}: {exc}"
        job.last_error = message
        job.updated_at = utc_now()
        # 达到尝试上限后保留失败详情供排查，不继续自动重试。
        if job.attempts >= job.max_attempts:
            job.status = ToolJobStatus.DEAD.value
            db.add(
                DeadLetterRecord(
                    job_id=job.id,
                    report_id=job.report_id,
                    kind=job.kind,
                    reason=message,
                    payload=json.dumps(
                        {"reportId": job.report_id, "kind": job.kind, "attempts": job.attempts},
                        ensure_ascii=False,
                    ),
                )
            )
        else:
            job.status = ToolJobStatus.PENDING.value
            job.run_after = utc_now() + timedelta(seconds=self.settings.tool_queue_retry_delay_seconds * max(1, job.attempts))
        db.add(job)
        db.commit()
        diagnostics.emit(
            "tool_job.dead_lettered" if job.status == ToolJobStatus.DEAD.value else "tool_job.retry_scheduled",
            job_id=job.id, report_id=job.report_id, job_kind=job.kind, attempt=job.attempts,
            reason_code="attempts_exhausted" if job.status == ToolJobStatus.DEAD.value else "attempt_failed",
        )

    def _recover_running_jobs(self) -> None:
        """用独立数据库会话恢复超过租约的执行中任务。

        仅恢复已过期任务，处理结束后关闭会话。
        """
        db = SessionLocal()
        try:
            ToolQueueService(db, self.settings).recover_expired_jobs()
        finally:
            db.close()


_worker: ToolQueueWorker | None = None


def get_tool_queue_worker(settings: Settings) -> ToolQueueWorker:
    """取得当前进程共享的工具队列工作器。

    首次调用使用传入配置创建，之后复用已有实例，不因新配置对象而自动重建。
    """
    global _worker
    if _worker is None:
        _worker = ToolQueueWorker(settings)
    return _worker
