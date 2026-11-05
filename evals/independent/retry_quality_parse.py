from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.services.ai import AiClient
from evals.config import get_eval_settings
from evals.quality.runner import QUALITY_DIMENSIONS, _judge_reply, compute_quality_summary

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "evals/independent/results/20261102-v1"


def is_parse_failure(result: dict) -> bool:
    return any(result["dimensionStats"][dimension]["mean"] == 0 for dimension in QUALITY_DIMENSIONS)


async def main() -> None:
    report = json.loads((RUN / "quality-recovered.json").read_text(encoding="utf-8"))
    base = get_eval_settings()
    settings = base.model_copy(update={
        "ai_provider": "openai",
        "openai_api_key": base.openai_api_key,
        "openai_base_url": base.openai_base_url,
        "openai_model": base.openai_model,
        "ai_temperature": 0.0,
        "ai_max_tokens": 4096,
    })
    client = AiClient(settings)
    retried = []
    for result in report["results"]:
        if not is_parse_failure(result):
            continue
        scores = await _judge_reply(client, result["text"], result["reply"])
        retried.append({"id": result["id"], "scores": {dimension: scores[dimension] for dimension in QUALITY_DIMENSIONS}})
        if any(scores[dimension] == 0 for dimension in QUALITY_DIMENSIONS):
            continue
        result["judgeRuns"] = [scores]
        result["dimensionStats"] = {
            dimension: {"mean": float(scores[dimension]), "std": 0.0}
            for dimension in QUALITY_DIMENSIONS
        }
        result["parseRetry"] = "Original zero-valued parse failure replaced by one successful retry; original report preserved separately."
    report.update(compute_quality_summary(report["results"]))
    report["parseRetryCount"] = len(retried)
    (RUN / "quality-final.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"retried": retried, "perDimension": report["perDimension"]}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
