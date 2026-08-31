from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    agent_framework: str = "langgraph"
    ai_provider: str = "ollama"
    ai_temperature: float = 0.35
    ai_max_tokens: int = 512
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "xling-qwen2.5-7b-ft:latest"
    ollama_classifier_model: str = "xling-cls-3b-ft:latest"
    finetuned_model_name: str = "xling-qwen2.5-7b-ft:latest"
    finetuned_model_dir: str = "models/xling-qwen2.5-7b-ft"
    finetuned_model_file: str = "xling-qwen2.5-7b-ft-q4_k_m.gguf"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_embedding_model: str = "text-embedding-3-small"
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    database_url: str = "mysql+pymysql://xling:xling@127.0.0.1:3306/xling?charset=utf8mb4"
    chat_history_limit: int = 10
    knowledge_top_k: int = 4
    knowledge_chunk_size: int = 512
    knowledge_chunk_overlap: int = 64
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
    review_timeout_minutes: int = 15
    review_timeout_poll_interval_seconds: float = 30.0
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    smtp_timeout_seconds: float = 10.0
    alert_email_delivery_mode: str = "log"
    alert_email_from: str = ""
    alert_email_to: str = ""
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
    # Security: bcrypt + JWT (issue 01: secure access migration)
    bcrypt_rounds: int = 12
    jwt_secret_key: str = "xling-dev-secret-change-in-production"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 1440
    # Risk trajectory (issue 07)
    risk_trajectory_session_window: int = 3
    risk_trajectory_cross_session_days: int = 7
    risk_trajectory_rising_threshold: int = 3
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[2]


@lru_cache
def get_settings() -> Settings:
    return Settings()
