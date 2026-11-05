from __future__ import annotations

from evals.config import get_eval_settings
from evals.quality.runner import QUALITY_DIMENSIONS, evaluate


def main() -> None:
    base = get_eval_settings()
    settings = base.model_copy(update={
        "quality_eval_dataset": "evals/independent/data/quality.json",
        "quality_eval_output": "evals/independent/results/20261102-v1/quality-recovered.json",
        "quality_eval_summary_output": "evals/independent/results/20261102-v1/quality-recovered-summary.json",
        "quality_eval_gen_provider": "openai",
        "quality_eval_gen_api_key": base.openai_api_key,
        "quality_eval_gen_base_url": base.openai_base_url,
        "quality_eval_gen_model": base.openai_model,
        "quality_eval_judge_provider": "openai",
        "quality_eval_judge_api_key": base.openai_api_key,
        "quality_eval_judge_base_url": base.openai_base_url,
        "quality_eval_judge_model": base.openai_model,
        "quality_eval_judge_runs": 1,
    })
    report = evaluate(settings)
    print(f"totalCases={report['totalCases']} judgeModel={report['judgeModel']} judgeRuns={report['judgeRuns']}")
    for dimension in QUALITY_DIMENSIONS:
        values = report["perDimension"][dimension]
        print(f"{dimension}: mean={values['mean']:.4f} std={values['std']:.4f}")


if __name__ == "__main__":
    main()
