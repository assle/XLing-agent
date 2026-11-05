from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_serializer, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


@dataclass(frozen=True)
class DecisionHeadPaths:
    bundle_dir: Path
    base_dir: Path
    adapter_dir: Path
    head_path: Path
    calibration_path: Path
    system_prompt_path: Path


class Settings(BaseSettings):
    environment: Literal["local", "test", "production"] = "local"
    ai_provider: str = "ollama"
    ai_temperature: float = 0.35
    ai_max_tokens: int = 2048
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "xling-qwen2.5-7b-ft:latest"
    ollama_classifier_model: str = "xling-general-cls-05b:quoted-f16"
    classifier_input_format: Literal["plain", "quoted"] = "quoted"
    # 单次前向决策头分类器为默认后端，ollama 生成式分类器保留为回退复核；测试经 conftest 固定为 ollama。
    classifier_backend: str = "decision_head"
    decision_head_bundle_dir: str = "models/xling-decision-head"
    decision_head_device: str = ""
    decision_head_max_length: int = 256
    # 分层安全策略阈值（工程初始值，待新鲜盲测集校准）：高风险直接判定、灰区复核、低置信度与前两名概率差回退。
    classifier_high_risk_threshold: float = Field(default=0.5, ge=0, le=1)
    classifier_high_risk_review_threshold: float = Field(default=0.15, ge=0, le=1)
    classifier_low_confidence_threshold: float = Field(default=0.70, ge=0, le=1)
    classifier_margin_threshold: float = Field(default=0.30, ge=0, le=1)
    finetuned_model_dir: str = "models/xling-qwen2.5-7b-ft"
    finetuned_model_file: str = "xling-qwen2.5-7b-ft-q4_k_m.gguf"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-4o-mini"
    openai_embedding_model: str = "text-embedding-3-small"
    embedding_base_url: str = ""
    embedding_api_key: SecretStr = SecretStr("")
    database_url: str = Field(
        default="mysql+pymysql://xling:xling@127.0.0.1:3306/xling?charset=utf8mb4",
        repr=False,
    )
    chat_history_limit: int = 10
    knowledge_top_k: int = 4
    # 线上检索策略：兼容值 multi_query_hybrid_rrf 实际为多查询 dense 主检索；normalized_fusion 为归一化加权。
    knowledge_retrieval_strategy: str = "multi_query_hybrid_rrf"
    knowledge_chunk_size: int = Field(default=512, gt=0)
    knowledge_chunk_overlap: int = Field(default=64, ge=0)
    knowledge_vector_enabled: bool = True
    knowledge_vector_required: bool = False
    bge_embedding_model: str = "BAAI/bge-m3"
    bge_reranker_model: str = "BAAI/bge-reranker-v2-m3"
    bge_candidate_pool: int = 20
    bge_rerank_enabled: bool = True
    bge_use_fp16: bool = False
    bge_device: str = "cpu"
    chroma_persist_dir: str = "data/chroma"
    chroma_collection_name: str = "xling_knowledge"
    embedding_timeout_seconds: float = 30.0
    excel_path: str = "data/xling-risk-ledger.xlsx"
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_memory_ttl_seconds: int = 86400
    redis_memory_max_messages: int = 40
    redis_socket_timeout_seconds: float = 2.0
    langgraph_checkpoint_backend: str = "async_sqlite"
    langgraph_checkpoint_path: str = "data/langgraph-checkpoints.db"
    langgraph_checkpoint_retention_days: int = 30
    diagnostic_log_dir: str = "data/logs"
    review_timeout_minutes: int = 15
    review_timeout_poll_interval_seconds: float = 30.0
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    smtp_timeout_seconds: float = 10.0
    alert_email_delivery_mode: str = "log"
    alert_email_from: str = ""
    alert_email_to: str = ""
    escalation_alert_email_to: str = ""
    alert_email_subject_prefix: str = "[Xling 高风险预警]"
    tool_queue_enabled: bool = True
    tool_queue_poll_interval_seconds: float = 1.0
    tool_queue_batch_size: int = 10
    tool_queue_max_attempts: int = 3
    tool_queue_retry_delay_seconds: float = 15.0
    tool_queue_lease_seconds: float = 300.0
    tool_queue_excel_workers: int = 1
    tool_queue_email_workers: int = 2
    alert_email_rate_limit_per_minute: int = 30
    # 密码校验强度及登录凭证的签名方式、有效时间。
    bcrypt_rounds: int = 12
    jwt_secret_key: SecretStr = SecretStr("xling-dev-secret-change-in-production")
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 1440
    # 风险轨迹的观察范围与连续上升判断阈值。
    risk_trajectory_session_window: int = 3
    risk_trajectory_cross_session_days: int = 7
    risk_trajectory_rising_threshold: int = 3
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        try:
            make_url(value)
        except ArgumentError as exc:
            raise ValueError("database_url must be a valid SQLAlchemy URL") from exc
        return value

    @model_validator(mode="after")
    def validate_classifier_thresholds(self):
        if self.classifier_high_risk_review_threshold > self.classifier_high_risk_threshold:
            raise ValueError(
                "classifier_high_risk_review_threshold must not exceed classifier_high_risk_threshold"
            )
        return self

    @model_validator(mode="after")
    def validate_knowledge_chunks(self):
        if self.knowledge_chunk_overlap >= self.knowledge_chunk_size:
            raise ValueError("knowledge_chunk_overlap must be smaller than knowledge_chunk_size")
        return self

    @model_validator(mode="after")
    def validate_smtp(self):
        if self.smtp_use_tls and self.smtp_use_ssl:
            raise ValueError("smtp_use_tls and smtp_use_ssl cannot both be enabled")
        if self.alert_email_delivery_mode.strip().lower() == "smtp" and not self.smtp_host.strip():
            raise ValueError("smtp_host is required when alert_email_delivery_mode is smtp")
        if bool(self.smtp_username.strip()) != bool(self.smtp_password.get_secret_value()):
            raise ValueError("smtp_username and smtp_password must be configured together")
        return self

    @model_validator(mode="after")
    def validate_provider(self):
        if self.ai_provider.lower() == "openai" and not self.openai_api_key.get_secret_value().strip():
            raise ValueError("openai_api_key is required when ai_provider is openai")
        return self

    @model_validator(mode="after")
    def validate_production_secrets(self):
        if self.environment != "production":
            return self
        jwt_secret = self.jwt_secret_key.get_secret_value()
        if not jwt_secret.strip() or jwt_secret == "xling-dev-secret-change-in-production":
            raise ValueError("jwt_secret_key must be replaced with a production secret")
        database = make_url(self.database_url)
        if database.get_backend_name() == "mysql" and (
            not database.username
            or not database.password
            or (database.username == "xling" and database.password == "xling")
        ):
            raise ValueError("database_url must contain non-development MySQL credentials in production")
        return self

    @property
    def effective_embedding_base_url(self) -> str:
        return self.embedding_base_url or self.openai_base_url

    @property
    def effective_embedding_api_key(self) -> str:
        return self.embedding_api_key.get_secret_value() or self.openai_api_key.get_secret_value()

    @property
    def safe_database_url(self) -> str:
        return make_url(self.database_url).render_as_string(hide_password=True)

    @field_serializer("database_url")
    def serialize_database_url(self, value: str) -> str:
        return make_url(value).render_as_string(hide_password=True)

    def resolve_path(self, value: str | Path) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else self.project_root / path

    @property
    def decision_head_paths(self) -> DecisionHeadPaths:
        bundle_dir = self.resolve_path(self.decision_head_bundle_dir)
        return DecisionHeadPaths(
            bundle_dir=bundle_dir,
            base_dir=bundle_dir / "base",
            adapter_dir=bundle_dir / "adapter",
            head_path=bundle_dir / "head.pt",
            calibration_path=bundle_dir / "calibration.json",
            system_prompt_path=bundle_dir / "system-prompt.txt",
        )

    @property
    def project_root(self) -> Path:
        """根据当前配置文件的位置定位项目根目录。

        返回绝对路径，不依赖启动命令所在的目录，供知识文件和模型文件定位使用。
        """
        return Path(__file__).resolve().parents[2]


@lru_cache
def get_settings() -> Settings:
    """读取应用配置并缓存所得的配置对象。

    首次调用时由 Settings 解析环境和配置文件，后续调用复用缓存；运行中改动环境不会自动刷新该缓存。
    """
    return Settings()
