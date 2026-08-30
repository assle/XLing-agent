from functools import lru_cache

from app.core.config import Settings


class EvalSettings(Settings):
    e2e_eval_dataset: str = "evals/e2e/xling-e2e-eval.jsonl"
    e2e_eval_output: str = "target/e2e-eval-report.json"
    e2e_eval_ai_provider: str = "mock"

    rag_eval_dataset: str = "evals/rag/xling-rag-eval.jsonl"
    rag_eval_output: str = "target/rag-eval-report.json"
    rag_eval_baseline_output: str = "target/rag-eval-baseline.json"
    rag_eval_database_url: str = "sqlite:///data/rag-eval.db"
    rag_eval_chroma_persist_dir: str = "data/chroma-eval"
    rag_eval_chroma_collection_name: str = "xling_knowledge_eval"
    rag_eval_chroma_snapshot_dir: str = "data/chroma-eval-snapshots"
    rag_eval_enabled: bool = False
    rag_eval_exit_after_run: bool = False
    rag_eval_multi_query_output: str = "target/rag-eval-multi-query.json"
    rag_eval_hybrid_rrf_output: str = "target/rag-eval-hybrid-rrf.json"
    rag_eval_llm_rerank_output: str = "target/rag-eval-llm-rerank.json"
    rag_eval_bge_output: str = "target/rag-eval-bge-m3.json"
    rag_eval_bge_summary_output: str = "target/rag-eval-bge-m3-summary.json"
    rag_eval_bge_rerank_output: str = "target/rag-eval-bge-m3-rerank.json"
    rag_eval_bge_rerank_summary_output: str = "target/rag-eval-bge-m3-rerank-summary.json"
    rag_eval_rerank_candidate_pool: int = 20
    rag_eval_multi_query_count: int = 3
    rag_eval_rrf_k: int = 60
    rag_eval_ai_provider: str = "mock"
    rag_eval_comparison_output: str = "target/rag-eval-comparison.json"
    rag_eval_comparison_md_output: str = "target/rag-eval-comparison.md"
    rag_eval_api_key: str = ""
    rag_eval_base_url: str = ""
    rag_eval_model: str = ""

    risk_eval_dataset: str = "evals/risk/xling-risk-eval.json"
    risk_eval_output: str = "target/risk-eval-report.json"
    risk_eval_summary_output: str = "target/risk-eval-summary.json"
    risk_eval_ai_provider: str = "mock"
    risk_eval_api_key: str = ""
    risk_eval_base_url: str = ""
    risk_eval_model: str = ""
    risk_calibration_dataset: str = "target/risk-calibration-inputs.json"
    risk_calibration_test_dataset: str = "target/risk-calibration-test-inputs.json"
    risk_calibration_source_dataset: str = "evals/risk/xling-risk-eval.json"
    risk_calibration_split: str = "evals/risk/risk-calibration-split.json"
    risk_calibration_output: str = "target/risk-calibration-report.json"
    risk_calibration_artifact_output: str = "target/risk-calibration-artifact.json"
    risk_conformal_alpha: float = 0.1

    quality_eval_dataset: str = "evals/quality/xling-quality-eval.json"
    quality_eval_output: str = "target/quality-eval-report.json"
    quality_eval_summary_output: str = "target/quality-eval-summary.json"
    quality_eval_gen_provider: str = "mock"
    quality_eval_gen_api_key: str = ""
    quality_eval_gen_base_url: str = ""
    quality_eval_gen_model: str = ""
    quality_eval_gen_temperature: float = 0.3
    quality_eval_judge_provider: str = "openai"
    quality_eval_judge_model: str = "gpt-4o"
    quality_eval_judge_base_url: str = "https://api.openai.com/v1"
    quality_eval_judge_api_key: str = ""
    quality_eval_judge_temperature: float = 0.0
    quality_eval_judge_runs: int = 3

    cls_eval_dataset: str = "finetune/data/val.jsonl"
    cls_eval_output: str = "target/cls-eval-report.json"
    cls_eval_summary_output: str = "target/cls-eval-summary.json"
    cls_eval_ai_provider: str = "mock"
    cls_eval_api_key: str = ""
    cls_eval_base_url: str = ""
    cls_eval_model: str = ""


@lru_cache
def get_eval_settings() -> EvalSettings:
    return EvalSettings()
