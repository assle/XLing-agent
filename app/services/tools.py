import smtplib
import ssl
import threading
from email.message import EmailMessage
from pathlib import Path

from openpyxl import Workbook, load_workbook
from sqlalchemy.orm import Session

from app.core import diagnostics
from app.core.config import Settings
from app.core.enums import ToolStatus
from app.models.entities import AlertRecord, ExcelRecord, SafetyAssessmentRecord, UserAccount

EXCEL_WRITE_LOCK = threading.Lock()


class ToolOrchestrationService:
    def __init__(self, db: Session, settings: Settings):
        """保存表格和通知工具使用的数据库会话及配置。

        初始化不创建文件或连接邮件服务器。
        """
        self.db = db
        self.settings = settings

    def write_excel(self, report: SafetyAssessmentRecord) -> ExcelRecord:
        """把安全评估记录追加到表格并登记写入结果。

        若数据库已有成功记录则直接复用；否则在进程内文件锁保护下打开或创建工作簿并保存。
        文件保存先于数据库提交，两者不共享事务，不能保证异常情况下绝不重复写行。
        """
        with diagnostics.stage("tool.excel", report_id=report.id):
            existing = (
                self.db.query(ExcelRecord)
                .filter(ExcelRecord.report_id == report.id, ExcelRecord.status == ToolStatus.SUCCESS.value)
                .first()
            )
            # 复用数据库中的成功结果，避免普通重复请求再次执行外部操作。
            if existing is not None:
                diagnostics.emit("tool.reused", tool="excel", report_id=report.id)
                return existing
            path = Path(self.settings.excel_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            # 此锁只协调当前进程里的表格操作，不能阻止另一个进程同时修改同一文件。
            with EXCEL_WRITE_LOCK:
                if path.exists():
                    workbook = load_workbook(path)
                    sheet = workbook.active
                else:
                    workbook = Workbook()
                    sheet = workbook.active
                    sheet.title = "Xling Risk Ledger"
                    sheet.append(["reportId", "riskLevel", "emotion", "confidence", "summary", "createdAt"])
                sheet.append([report.id, report.risk_level, report.emotion, report.confidence, report.summary, report.created_at.isoformat()])
                # 先把工作簿写到文件系统，下面才登记数据库结果；异常时两者可能处于不同状态。
                workbook.save(path)
            record = ExcelRecord(report_id=report.id, file_path=str(path), status=ToolStatus.SUCCESS.value, message="Excel 台账已写入")
            self.db.add(record)
            self.db.commit()
            return record

    def notify(self, report: SafetyAssessmentRecord) -> AlertRecord:
        """根据投递模式处理风险通知并保存结果。

        已有成功记录时复用；log 模式只登记，smtp 模式通过邮件服务器发送，未知模式或配置缺失登记失败。
        邮件发送异常转为失败记录，调用方须检查返回状态。
        """
        with diagnostics.stage("tool.notification", report_id=report.id):
            existing = (
                self.db.query(AlertRecord)
                .filter(AlertRecord.report_id == report.id, AlertRecord.status == ToolStatus.SUCCESS.value)
                .first()
            )
            if existing is not None:
                diagnostics.emit("tool.reused", tool="notification", report_id=report.id)
                return existing
            recipient = self.settings.alert_email_to.strip() or "unconfigured"
            mode = self.settings.alert_email_delivery_mode.strip().lower()
            # 记录模式没有外部邮件投递；成功表示通知已记录。
            if mode == "log":
                return self._save_alert(
                    report,
                    recipient if recipient != "unconfigured" else "log",
                    ToolStatus.SUCCESS.value,
                    f"高风险预警已记录：reportId={report.id}，deliveryMode=log",
                )
            if mode != "smtp":
                diagnostics.degraded("tool.notification", "unknown_delivery_mode", report_id=report.id)
                return self._save_alert(
                    report,
                    recipient,
                    ToolStatus.FAILED.value,
                    f"高风险预警邮件未发送：未知投递模式 {self.settings.alert_email_delivery_mode}",
                )
            # 先检查必要配置，避免缺少地址时仍尝试连接邮件服务器。
            missing = self._missing_email_config()
            if missing:
                diagnostics.degraded("tool.notification", "missing_email_configuration", report_id=report.id)
                return self._save_alert(
                    report,
                    recipient,
                    ToolStatus.FAILED.value,
                    f"高风险预警邮件未发送：缺少配置 {', '.join(missing)}",
                )
            try:
                # 只有真实发送模式且配置齐全才进入邮件连接过程。
                self._send_alert_email(report)
            except Exception as exc:
                diagnostics.degraded("tool.notification", "email_delivery_failed", exc, report_id=report.id)
                return self._save_alert(
                    report,
                    recipient,
                    ToolStatus.FAILED.value,
                    f"高风险预警邮件发送失败：{type(exc).__name__}: {exc}",
                )
            return self._save_alert(report, recipient, ToolStatus.SUCCESS.value, f"高风险预警邮件已发送：reportId={report.id}")

    def _save_alert(self, report: SafetyAssessmentRecord, recipient: str, status: str, message: str) -> AlertRecord:
        """保存通知的收件人、状态和结果文字。

        提交数据库后返回记录；状态由调用方传入，本方法不执行邮件投递。
        """
        record = AlertRecord(
            report_id=report.id,
            channel="email",
            recipient=recipient,
            status=status,
            message=message,
        )
        self.db.add(record)
        self.db.commit()
        return record

    def _missing_email_config(self) -> list[str]:
        """检查邮件发送所需的服务器、发件人和收件人配置。

        返回缺失项名称，不验证网络连通性或账号密码是否正确。
        """
        missing = []
        if not self.settings.smtp_host.strip():
            missing.append("SMTP_HOST")
        if not self._sender():
            missing.append("ALERT_EMAIL_FROM 或 SMTP_USERNAME")
        if not self._recipients():
            missing.append("ALERT_EMAIL_TO")
        return missing

    def _send_alert_email(self, report: SafetyAssessmentRecord) -> None:
        """组装风险通知并通过配置的邮件连接发送。

        优先采用建立连接即加密的方式，否则可按配置升级为加密连接；连接用完后自动关闭。
        """
        message = EmailMessage()
        message["Subject"] = f"{self.settings.alert_email_subject_prefix} reportId={report.id}"
        message["From"] = self._sender()
        message["To"] = ", ".join(self._recipients())
        message.set_content(self._email_body(report))

        context = ssl.create_default_context()
        if self.settings.smtp_use_ssl:
            with smtplib.SMTP_SSL(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=self.settings.smtp_timeout_seconds,
                context=context,
            ) as server:
                self._send_message(server, message)
            return

        with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=self.settings.smtp_timeout_seconds) as server:
            server.ehlo()
            if self.settings.smtp_use_tls:
                server.starttls(context=context)
                server.ehlo()
            self._send_message(server, message)

    def _send_message(self, server: smtplib.SMTP, message: EmailMessage) -> None:
        """在已有邮件连接上按需登录并提交邮件。

        只有配置用户名时才登录；服务器接受邮件不等于收件人已经阅读或最终投递成功。
        """
        if self.settings.smtp_username:
            server.login(self.settings.smtp_username, self.settings.smtp_password)
        server.send_message(message)

    def _email_body(self, report: SafetyAssessmentRecord) -> str:
        """根据安全评估记录和关联用户信息构造邮件正文。

        当前正文包含用户标识、摘要及原始消息；不调用脱敏器，不应把它描述为脱敏摘要。
        """
        user = self.db.get(UserAccount, report.user_id)
        username = user.username if user else f"userId={report.user_id}"
        display_name = user.display_name if user else ""
        return "\n".join(
            [
                "Xling 检测到一条高风险安全预警，请尽快由授权审核团队安排跟进。",
                "",
                f"报告ID：{report.id}",
                f"学生：{display_name} ({username})" if display_name else f"学生：{username}",
                f"风险等级：{report.risk_level}",
                f"情绪标签：{report.emotion}",
                f"置信度：{report.confidence}",
                f"摘要：{report.summary}",
                f"创建时间：{report.created_at.isoformat()}",
                "",
                "学生原始消息：",
                report.content,
            ]
        )

    def _sender(self) -> str:
        """选择通知发件人地址。

        独立发件人配置优先，否则使用邮件登录用户名，并去除两端空白。
        """
        return self.settings.alert_email_from.strip() or self.settings.smtp_username.strip()

    def _recipients(self) -> list[str]:
        """将逗号或分号分隔的收件人配置转换成列表。

        去掉空白与空项，不在此验证地址格式或去重。
        """
        normalized = self.settings.alert_email_to.replace(";", ",")
        return [recipient.strip() for recipient in normalized.split(",") if recipient.strip()]
